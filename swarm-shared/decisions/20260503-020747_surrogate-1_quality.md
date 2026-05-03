# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side HF API call) listing date-folders and parquet files. Embeds this list at workflow dispatch time or reads from repo if present.
- Deterministically hashes each file path to assign to shards (`hash(slug) % SHARD_TOTAL`), so every shard processes a stable disjoint slice.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429 limits).
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store (same as Space) and writes non-duplicate pairs to:
  - `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Exits with success/failure codes; GitHub Actions retries on 429 with 360s backoff.

Changes:
1. `bin/dataset-enrich.py` — new worker (replaces shell script).
2. `bin/dataset-enrich.sh` — thin wrapper for backward compat (calls python).
3. `.github/workflows/ingest.yml` — minor tweaks to pass matrix vars and optional file-list artifact.
4. `requirements.txt` — ensure `requests`, `pyarrow`, `datasets`, `huggingface_hub`, `numpy`.

---

## Code Snippets

### 1. `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out-dir batches/public-merged

Environment:
  HF_TOKEN         Optional; not required for CDN downloads.
  FILE_LIST_JSON   Optional path to file-list.json (Mac-generated).
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
RETRY_429_WAIT = 360
MAX_RETRIES = 3


def deterministic_shard(file_path: str, total: int) -> int:
    """Stable shard assignment by file path."""
    h = hashlib.md5(file_path.encode()).hexdigest()
    return int(h, 16) % total


def load_file_list(repo: str, date: str, file_list_json: Optional[str]) -> List[str]:
    """
    Load list of parquet files for a date folder.
    Expected format: list of paths relative to dataset root, e.g.
      ["2026-05-03/file1.parquet", "2026-05-03/file2.parquet"]
    """
    if file_list_json and Path(file_list_json).exists():
        with open(file_list_json) as f:
            data = json.load(f)
        if isinstance(data, dict) and "files" in data:
            files = data["files"]
        else:
            files = data
        return [p for p in files if p.startswith(date) and p.endswith(".parquet")]

    # Fallback: list repo tree for the date folder (single API call)
    # This may hit rate limits; prefer pre-generated file-list.json.
    try:
        from huggingface_hub import list_repo_tree
    except ImportError:
        raise RuntimeError("huggingface_hub required for fallback file listing")

    tree = list_repo_tree(repo, recursive=False, path=date)
    files = [item.path for item in tree if item.path.endswith(".parquet")]
    return files


def cdn_download(url: str, timeout: int = 30) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers={})
            if resp.status_code == 429:
                wait = RETRY_429_WAIT
                print(f"CDN 429, waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2**attempt
            print(f"Download failed: {e}, retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Unreachable")


def project_to_pair(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous record to {prompt, response}.
    Tolerates common field names; returns None if unusable.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}


def process_parquet_cdn(repo: str, file_path: str, dedup: DedupStore) -> List[Dict[str, str]]:
    pairs = []
    url = f"{HF_DATASETS_CDN}/{repo}/resolve/main/{file_path}"
    data = cdn_download(url)

    try:
        table = pq.read_table(pa.BufferReader(data))
    except Exception as e:
        print(f"Failed to read parquet {file_path}: {e}")
        return pairs

    # Convert to list of dicts (pyarrow RecordBatch -> python)
    # Use to_pylist() for schema-flexible rows.
    rows = table.to_pylist()
    for row in rows:
        pair = project_to_pair(row or {})
        if not pair:
            continue

        # Deterministic md5 for dedup (same as Space)
        payload = f"{pair['prompt']}\n{pair['response']}".encode()
        md5 = hashlib.md5(payload).hexdigest()
        if dedup.seen(md5):
            continue

        dedup.add(md5)
        pairs.append(pair)

    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="Surrogate-1 CDN-bypass worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    parser.add_argument("--out-dir", default="batches/public-merged")
    parser.add_argument("--file-list", help="Path to file-list.json (optional)")
    args = parser.parse_args()

    shard_id = int(os.environ.get("SHARD_ID", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))
    if not (0 <= shard_id < shard_total):
        print(f"Invalid SHARD_ID={shard_id} SHARD_TOTAL={shard_total}")
        sys.exit(1)

    dedup = DedupStore()
    files = load_file_list(args.repo, args.date, args.file_list)
    assigned = [f for f in files if deterministic_shard(f, shard_total) == shard_id]

    print(f"Shard {shard_id}/{shard_total} assigned {len(assigned)} files")

    out_dir = Path(args.out_dir) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_file = out_dir / f"shard{shard_id}-{ts}.jsonl"

    all_pairs: List[Dict[str, str]] = []
    for fp in
