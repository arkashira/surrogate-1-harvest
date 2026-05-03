# surrogate-1 / discovery

## Implementation Plan (≤2 h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, one `list_repo_tree` call per date folder) to enumerate target files; embeds the list so Lightning training does zero HF API calls during data load.
- Downloads only assigned shard files via HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429 limits).
- Projects each file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` on mixed-schema repos).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Writes normalized pairs to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Keeps the same GitHub Actions matrix (16 shards) and `HF_TOKEN` secret for final push.

Estimated effort:
- `bin/dataset-enrich.py` — 90 min (robust CDN fetch, schema projection, dedup, retry/backoff).
- Update `.github/workflows/ingest.yml` to invoke python and pass matrix — 15 min.
- Smoke test + chmod + small README note — 15 min.

---

## Code Snippets

### 1) bin/dataset-enrich.py (new)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public-dataset shards.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --file-list file-list.json \
    --out-dir batches/public-merged

Behavior:
- Reads file-list.json (created by Mac-side list_repo_tree for one date folder).
- Assigns files to shards by hash(slug) % SHARD_TOTAL.
- Downloads assigned files via HF CDN (no auth header) to bypass /api/ rate limits.
- Projects each file to {prompt, response} at parse time.
- Deduplicates via lib.dedup.DedupStore.
- Writes shard-N-<ts>.jsonl for final push.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

# local
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_BACKOFF = [1, 2, 4, 8, 16]
TIMEOUT = httpx.Timeout(60.0, connect=30.0)


def shard_for_file(slug: str, total: int) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.sha256(slug.encode()).digest()
    return int.from_bytes(digest, "little") % total


def download_cdn(url: str, dest: Path) -> bool:
    for wait in RETRY_BACKOFF:
        try:
            resp = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception as exc:
            print(f"[WARN] CDN fetch failed ({url}): {exc}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return False


def parse_file_to_pairs(local_path: Path) -> Iterable[Dict[str, str]]:
    """
    Project heterogeneous HF dataset files to {prompt, response}.
    Supports:
      - parquet (preferred)
      - json/jsonl
    Returns iterable of dicts with keys 'prompt' and 'response'.
    """
    suffix = local_path.suffix.lower()
    try:
        if suffix == ".parquet":
            table = pq.read_table(local_path, columns=["prompt", "response"])
            df = table.to_pandas()
            for _, row in df.iterrows():
                prompt = str(row.get("prompt") or "")
                response = str(row.get("response") or "")
                if prompt and response:
                    yield {"prompt": prompt.strip(), "response": response.strip()}
            return

        # json / jsonl
        content = local_path.read_text(encoding="utf-8")
        # try jsonl first
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        for ln in lines:
            obj = json.loads(ln)
            prompt = str(obj.get("prompt") or obj.get("input") or "")
            response = str(obj.get("response") or obj.get("output") or "")
            if prompt and response:
                yield {"prompt": prompt.strip(), "response": response.strip()}
        return
    except Exception as exc:
        print(f"[WARN] parse failed {local_path}: {exc}", file=sys.stderr)
        return


def build_file_list(repo: str, date: str, file_list_path: Path) -> List[Dict[str, Any]]:
    """Load pre-generated file list for a date folder."""
    if not file_list_path.is_file():
        raise FileNotFoundError(f"file-list not found: {file_list_path}")
    data = json.loads(file_list_path.read_text())
    # Expected shape: list of {"path": "...", "slug": "..."} or dict with date key
    if isinstance(data, dict):
        data = data.get(date, [])
    if not isinstance(data, list):
        raise ValueError("file-list must be list or dict[date]=list")
    # Normalize entries
    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"path": item, "slug": item})
        else:
            out.append({"path": item["path"], "slug": item.get("slug", item["path"])})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass shard worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder in dataset")
    parser.add_argument("--file-list", type=Path, required=True, help="Pre-generated file-list.json")
    parser.add_argument("--out-dir", type=Path, default=Path("batches/public-merged"))
    parser.add_argument("--shard-id", type=int, default=int(os.environ.get("SHARD_ID", 0)))
    parser.add_argument("--shard-total", type=int, default=int(os.environ.get("SHARD_TOTAL", 16)))
    parser.add_argument("--hf-token", help="HF write token (for push)")
    args = parser.parse_args()

    if args.shard_id < 0 or args.shard_id >= args.shard_total:
        print(f"[ERROR] Invalid SHARD_ID={args.shard_id} for SHARD_TOTAL={args.shard_total}", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{args.shard_id}-{ts}.jsonl"

    dedup = DedupStore()
    files = build_file_list(args.repo, args.date, args.file_list)
    assigned = [f for f in files if shard_for_file(f["slug"], args.shard_total) == args.shard_id]

    print(f"[INFO] Shard {args.shard_id}/{args.shard_total}: processing {len(assigned)} files")

    written = 0
    skipped_dup = 0
    failed_files = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for entry in tqdm(assigned, desc=f"Shard {args.shard_id}"):
            cdn_url = HF_CDN_TEMPLATE.format(repo=args.repo, path=entry
