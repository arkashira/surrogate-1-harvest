# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, one API call per date folder) to enumerate files; embeds this list so Lightning training does **zero HF API calls** during data load.
- Downloads via HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header — bypasses `/api/` rate limits entirely.
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes to deterministic shard output:
  ```
  batches/public-merged/<YYYY-MM-DD>/shard<SHARD_ID>-<HHMMSS>.jsonl
  ```
- Commits via HF Hub (token from `HF_TOKEN`) using deterministic filenames to avoid collisions across shards/iterations.
- Reuses existing HF Space pattern: lightweight orchestration only; heavy decode/transform in isolated GitHub runners (7 GB each × 16).

### Steps (timed)

1. **Create `bin/file-list.json` template** (5 min) — date folder from today, recursive=False tree.
2. **Write `bin/dataset-enrich.py`** (60–80 min) — CDN fetch, schema projection, dedup, shard output.
3. **Update `.github/workflows/ingest.yml`** (10 min) — set matrix, pass `SHARD_ID`, `SHARD_TOTAL`, date.
4. **Smoke test one shard locally** (10–15 min) — verify JSONL output and dedup behavior.
5. **Commit & push** (5 min).

---

## `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out-dir batches

Behavior:
- Reads file-list.json for the target date folder.
- Assigns files to shards by hash(slug) % SHARD_TOTAL.
- Downloads each assigned file via HF CDN (no auth header).
- Projects to {prompt, response} at parse time.
- Deduplicates via lib.dedup using md5 hash.
- Writes shard-N.jsonl and commits to repo.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import CommitOperationAdd, HfApi

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, register_hash  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
RETRY_WAIT = 360  # seconds after 429
MAX_RETRIES = 3

def _hash_slug(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)

def _assign_shard(slug: str, total: int) -> int:
    return _hash_slug(slug) % total

def _download_cdn(url: str, timeout: int = 30) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code == 429:
                    wait = RETRY_WAIT
                    print(f"[{url}] 429 rate-limited, waiting {wait}s (attempt {attempt})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                wait = RETRY_WAIT
                print(f"[{url}] 429 rate-limited, waiting {wait}s (attempt {attempt})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed to download {url} after {MAX_RETRIES} attempts")

def _project_to_pair(record: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file record to {prompt, response}.
    Tolerates common field variants.
    """
    prompt = record.get("prompt") or record.get("input") or record.get("question") or record.get("text")
    response = record.get("response") or record.get("output") or record.get("answer") or record.get("completion")

    if prompt is None or response is None:
        # If record is flat string or unexpected shape, skip
        return None

    return {"prompt": str(prompt), "response": str(response)}

def _compute_md5(pair: Dict[str, str]) -> str:
    blob = json.dumps(pair, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.md5(blob).hexdigest()

def _read_file_via_cdn(repo: str, cdn_path: str) -> List[Dict[str, str]]:
    url = f"{HF_DATASETS_CDN}/{repo}/resolve/main/{cdn_path}"
    data = _download_cdn(url)

    suffix = Path(cdn_path).suffix.lower()
    pairs: List[Dict[str, str]] = []

    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(data))
            for batch in table.to_batches(max_chunksize=8192):
                for i in range(batch.num_rows):
                    rec = {col: batch[col][i].as_py() for col in batch.schema.names}
                    pair = _project_to_pair(rec)
                    if pair:
                        pairs.append(pair)
        elif suffix in (".json", ".jsonl"):
            text = data.decode("utf-8", errors="replace")
            # Try JSON lines first
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines:
                try:
                    rec = json.loads(ln)
                    pair = _project_to_pair(rec)
                    if pair:
                        pairs.append(pair)
                except json.JSONDecodeError:
                    continue
            # If no lines parsed, try single JSON array/object
            if not pairs and lines:
                try:
                    rec = json.loads(text)
                    if isinstance(rec, list):
                        for item in rec:
                            pair = _project_to_pair(item if isinstance(item, dict) else {"text": item})
                            if pair:
                                pairs.append(pair)
                    else:
                        pair = _project_to_pair(rec if isinstance(rec, dict) else {"text": rec})
                        if pair:
                            pairs.append(pair)
                except json.JSONDecodeError:
                    pass
        else:
            # Fallback: treat as raw text
            text = data.decode("utf-8", errors="replace")
            pair = _project_to_pair({"text": text})
            if pair:
                pairs.append(pair)
    except Exception as exc:
        print(f"Failed to parse {cdn_path}: {exc}", file=sys.stderr)

    return pairs

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass shard worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", default=datetime.utcnow().strftime("%Y-%m-%d"))
    parser.add_argument("--out-dir", default="batches")
    parser.add_argument("--manifest", default="file-list.json")
    parser.add_argument("--shard-id", type=int, default=int(os.environ.get("SHARD_ID", 0)))
    parser.add_argument("--shard-total", type=int, default=int(os.environ.get("SHARD_TOTAL", 16)))
    parser.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"))
    args = parser.parse_args()

    if args.shard_total <= 0 or args.sh
