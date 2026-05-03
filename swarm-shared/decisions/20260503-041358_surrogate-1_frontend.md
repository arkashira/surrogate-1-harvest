# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that:
- eliminates Hugging Face API rate limits during training data loads,
- prevents mixed-schema `pyarrow` `CastError`s by projecting to `{prompt, response}` only at parse time,
- uses a single `list_repo_tree` call (saved to JSON) so Lightning training does **CDN-only fetches with zero API calls** during data load.

### Steps (1h 45m total)

1. **Create manifest generator** (`bin/build_manifest.py`) — 25m  
   - Runs on Mac (or cron) after rate-limit window clears.
   - Calls `list_repo_tree(path, recursive=False)` for one date folder.
   - Emits `manifest.json` with `{ "date": "...", "files": [ { "path": "...", "size": ..., "etag": ... } ] }`.
   - Commits or uploads alongside training code.

2. **Create CDN-only dataset loader** (`src/cdn_dataset.py`) — 30m  
   - Reads `manifest.json`.
   - Downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth header).
   - Streams each file; projects to `{prompt, response}` at parse time.
   - Yields normalized records; skips malformed rows.

3. **Update worker script** (`bin/dataset-enrich.sh` → `bin/dataset-enrich.py`) — 35m  
   - Keep same CLI contract so workflow doesn’t change.
   - Accept `SHARD_ID`, `TOTAL_SHARDS`, `DATE_FOLDER`.
   - Use manifest to list files; deterministic hash-slug → shard assignment.
   - Stream-normalize-dedup-upload to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
   - Use `lib/dedup.py` for central md5 store (SQLite) to avoid cross-run duplicates.

4. **Update training script** (`train.py`) — 25m  
   - Import `cdn_dataset`.
   - Load via `IterableDataset` so Lightning does zero HF API calls during training.
   - Add `--manifest` flag (default `manifest.json`).

5. **Update workflow** (`.github/workflows/ingest.yml`) — 10m  
   - No matrix changes; just update the run command to use new Python worker.
   - Ensure `requirements.txt` includes `requests`, `tqdm`, `pyarrow`, `datasets`, `huggingface_hub`.

---

## Code Snippets

### 1. `bin/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Build manifest for a date folder to enable CDN-only training.
Usage:
  python build_manifest.py --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 --out manifest.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Folder name, e.g. 2026-05-03")
    parser.add_argument("--out", default="manifest.json")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"))
    args = parser.parse_args()

    api = HfApi(token=args.token)
    # List only top-level of the date folder (non-recursive)
    entries = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)

    files = []
    for e in entries:
        if e.type != "file":
            continue
        # Expecting parquet/jsonl; skip others
        if not (e.path.endswith(".parquet") or e.path.endswith(".jsonl")):
            continue
        files.append(
            {
                "path": e.path,
                "size": e.size or 0,
                "etag": getattr(e, "etag", None),
            }
        )

    manifest = {"repo": args.repo, "date": args.date, "files": files}
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

### 2. `src/cdn_dataset.py`

```python
import json
import os
from typing import Dict, Iterator, Any
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def stream_parquet_to_dicts(file_url: str) -> Iterator[Dict[str, Any]]:
    """Stream a remote parquet file and project to {prompt, response}."""
    # Use pyarrow.dataset/ParquetFile via HTTP range requests
    try:
        pf = pq.ParquetFile(file_url, memory_map=False)
        for batch in pf.iter_batches(batch_size=1024, columns=["prompt", "response"]):
            df = batch.to_pydict()
            for prompt, response in zip(df.get("prompt", []), df.get("response", [])):
                if prompt is None or response is None:
                    continue
                yield {"prompt": str(prompt), "response": str(response)}
    except Exception as exc:
        # Defensive: skip malformed files rather than crash training
        print(f"Skipping {file_url}: {exc}", file=sys.stderr)
        return

def stream_jsonl_to_dicts(file_url: str) -> Iterator[Dict[str, Any]]:
    with requests.get(file_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    yield {"prompt": str(prompt), "response": str(response)}
            except Exception:
                continue

def build_cdn_dataset(manifest_path: str) -> Iterator[Dict[str, Any]]:
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    for meta in manifest["files"]:
        path = meta["path"]
        url = cdn_url(repo, path)
        if path.endswith(".parquet"):
            yield from stream_parquet_to_dicts(url)
        elif path.endswith(".jsonl"):
            yield from stream_jsonl_to_dicts(url)
```

### 3. `bin/dataset-enrich.py` (replaces shell worker)

```python
#!/usr/bin/env python3
"""
CDN-based worker for surrogate-1 public dataset enrichment.
Usage (via workflow matrix):
  SHARD_ID=0 TOTAL_SHARDS=16 DATE=2026-05-03 python dataset-enrich.py
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

from src.cdn_dataset import build_cdn_dataset
from lib.dedup import DedupStore  # central md5 store

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, default=int(os.getenv("SHARD_ID", 0)))
    parser.add_argument("--total", type=int, default=int(os.getenv("TOTAL_SHARDS", 16)))
    parser.add_argument("--date", default=os.getenv("DATE", ""))
    parser.add_argument("--manifest", default="manifest.json")
    parser.add_argument("--out-dir", default="batches/public-merged")
    parser.add_argument("--hf-repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    args = parser.parse_args()

    if not args.date:
        print("ERROR: DATE or --date required", file=sys.stderr
