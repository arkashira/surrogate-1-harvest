# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — deterministic shard worker that:
   - Accepts `SHARD_ID`/`SHARD_TOTAL` env vars (matrix)
   - Uses a single `list_repo_tree` call (date folder) from Mac to produce `file-manifest.json`
   - Filters files by `slug_hash % SHARD_TOTAL == SHARD_ID`
   - Downloads via **HF CDN** (`resolve/main/...`) with no Authorization header (bypasses API rate limit)
   - Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
   - Dedups via central md5 store (`lib/dedup.py`)
   - Emits `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`

2. **Add `bin/gen-manifest.py`** (run from Mac) — one-time API call to list date folder and save `file-manifest.json` to repo root; embed in worker so Lightning training can do CDN-only fetches with zero API calls.

3. **Update `.github/workflows/ingest.yml`** — switch from shell script to `python bin/worker.py`, pass matrix `shard_id`/`shard_total`, set `SHELL=/bin/bash` for any cron wrappers.

4. **Update `requirements.txt`** — add `requests`, keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

5. **Remove/Deprecate** direct `dataset-enrich.sh` streaming usage (keep for fallback).

---

## Code Snippets

### `bin/gen-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate file manifest for a date folder to avoid recursive list_repo_files.
Run from Mac after HF API rate-limit window clears.
Usage:
  HF_TOKEN=... python bin/gen-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out file-manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

def slug_hash(s: str) -> int:
    # deterministic, stable across runs
    return hash(s) & 0x7fffffff

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    parser.add_argument("--out", default="file-manifest.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    # list single folder (non-recursive) to avoid pagination explosion
    try:
        items = api.list_repo_tree(
            repo_id=args.repo,
            path=args.date,
            recursive=False,
        )
    except Exception as e:
        print(f"HF API error: {e}", file=sys.stderr)
        # fallback: allow empty manifest and rely on CDN discovery via prefix
        items = []

    files = []
    for item in items:
        if not item.rfilename:
            continue
        # expect files like 2026-05-03/<slug>.jsonl or .parquet
        path = f"{args.date}/{item.rfilename}"
        files.append({
            "path": path,
            "slug": item.rfilename,
            "shard_key": slug_hash(item.rfilename),
            "cdn_url": f"https://huggingface.co/datasets/{args.repo}/resolve/main/{path}",
        })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
Deterministic shard worker for public-dataset ingest.
Uses CDN-bypass to avoid HF API rate limits.
"""
import json
import os
import sys
import hashlib
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from datasets import load_dataset

# local
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa

HF_TOKEN = os.environ.get("HF_TOKEN", "")
REPO = "axentx/surrogate-1-training-pairs"
DATE = os.environ.get("INGEST_DATE", datetime.utcnow().strftime("%Y-%m-%d"))
MANIFEST = os.environ.get("MANIFEST", "file-manifest.json")

SHARD_ID = int(os.environ.get("SHARD_ID", 0))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", 16))

OUT_DIR = Path(f"batches/public-merged/{DATE}")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def slug_hash(s: str) -> int:
    return hash(s) & 0x7fffffff

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % SHARD_TOTAL == SHARD_ID

def safe_download(url: str, timeout: int = 30) -> bytes:
    # CDN URLs do not require Authorization header
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def project_to_pair(obj) -> dict:
    """
    Project heterogeneous file schemas to {prompt, response} only.
    Accepts dict-like rows from jsonl or parquet.
    """
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
    # normalize
    if not isinstance(prompt, str):
        prompt = json.dumps(prompt) if prompt else ""
    if not isinstance(response, str):
        response = json.dumps(response) if response else ""
    return {"prompt": prompt.strip(), "response": response.strip()}

def md5_hash_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()

def process_file(path: str, dedup: DedupStore) -> list[dict]:
    url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"
    data = safe_download(url)
    pairs = []

    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".jsonl":
            for line in data.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                pair = project_to_pair(obj)
                raw = line.encode("utf-8")
                h = md5_hash_bytes(raw)
                if not dedup.seen(h):
                    dedup.add(h)
                    pairs.append(pair)

        elif suffix == ".parquet":
            # Use pyarrow to read in-memory bytes; avoids mixed-schema load_dataset issues
            table = pq.read_table(pa.BufferReader(data))
            # Convert to list of dicts row-wise
            cols = table.column_names
            for i in range(table.num_rows):
                row = {c: table[c][i].as_py() for c in cols}
                pair = project_to_pair(row)
                # stable hash: serialize row deterministically
                raw = json.dumps(row, sort_keys=True).encode("utf-8")
                h = md5_hash_bytes(raw)
                if not dedup.seen(h):
                    dedup.add(h)
                    pairs.append(pair)
        else:
            # fallback: try load_dataset on single file (slower)
            # but prefer CDN + projection above
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=suffix) as f:
                f.write(data)
                f.flush()
                ds = load_dataset("json", data_files=f.name, split="train")
                for obj in ds:

