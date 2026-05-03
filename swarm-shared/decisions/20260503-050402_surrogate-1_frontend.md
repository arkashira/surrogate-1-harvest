# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Changes

1. `bin/dataset-enrich.sh` → `bin/dataset-enrich.py`
   - Single API call from Mac (after rate-limit window) to `list_repo_tree(path, recursive=False)` for one date folder → save `manifest.json`
   - Worker uses **CDN-only fetches** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header → bypasses `/api/` rate limits entirely
   - Per-file streaming parse with schema projection to `{prompt, response}` only; drop `source`, `ts`, extra cols
   - Deterministic shard assignment via `hash(slug) % 16`; each runner processes only its `SHARD_ID` slice
   - Central md5 dedup via existing `lib/dedup.py` SQLite store (shared across runners via HF Space)
   - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` (one JSON object per line)

2. `.github/workflows/ingest.yml`
   - Keep 16-shard matrix, but invoke `python bin/dataset-enrich.py --shard ${{ matrix.shard }}`
   - Add `timeout-minutes: 30` and retry on 429 with 360s backoff
   - Cache `manifest.json` per date to avoid repeated `list_repo_tree` calls within the same workflow run

3. `requirements.txt`
   - Add: `requests`, `tqdm`, `python-slugify`
   - Keep: `datasets`, `huggingface_hub`, `pyarrow`, `numpy`

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-first, CDN-only ingestion worker for surrogate-1.
Usage: python bin/dataset-enrich.py --shard N --date 2026-05-03
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download
from tqdm import tqdm

from lib.dedup import DedupStore  # existing central md5 store

HF_REPO = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
API = HfApi()

# Rate-limit safety: wait 360s on 429 (per pattern)
def safe_get(url, retries=3, backoff=360):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 429:
                if attempt < retries - 1:
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"Rate limited on {url}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)

def list_date_folder(date_str: str):
    """Single API call to list one date folder (non-recursive)."""
    items = API.list_repo_tree(repo_id=HF_REPO, path=f"raw/{date_str}", recursive=False)
    # items can be dict or object; normalize
    files = []
    for item in items:
        path = item.get("path", item) if isinstance(item, dict) else getattr(item, "path", str(item))
        if path and not path.endswith("/"):
            files.append(path)
    return files

def project_to_pair(file_path: str):
    """Stream one file via CDN, project to {prompt,response}, yield pairs."""
    url = f"{CDN_ROOT}/{file_path}"
    r = safe_get(url)
    # Try parquet first (common), fallback to jsonl
    if file_path.endswith(".parquet"):
        import pyarrow.parquet as pq
        import pyarrow as pa
        # Use streaming to avoid schema issues; project only needed cols
        try:
            pf = pq.ParquetFile(file_path)  # will fail without local file
        except Exception:
            # Download to temp via hf_hub_download (CDN under the hood) or bytes
            local_path = hf_hub_download(repo_id=HF_REPO, filename=file_path, repo_type="dataset")
            pf = pq.ParquetFile(local_path)

        for batch in pf.iter_batches(batch_size=500, columns=["prompt", "response"]):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                prompt = str(row.get("prompt") or row.get("input") or "")
                response = str(row.get("response") or row.get("output") or "")
                if prompt and response:
                    yield {"prompt": prompt.strip(), "response": response.strip()}
    else:
        # Assume JSONL / JSON
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            prompt = str(obj.get("prompt") or obj.get("input") or "")
            response = str(obj.get("response") or obj.get("output") or "")
            if prompt and response:
                yield {"prompt": prompt.strip(), "response": response.strip()}

def slug_for(pair):
    return hashlib.md5(f"{pair['prompt'][:128]}|{pair['response'][:128]}".encode()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True, help="Shard ID 0..15")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--out-dir", default="batches/public-merged")
    args = parser.parse_args()

    if not (0 <= args.shard <= 15):
        print("Shard must be 0..15", file=sys.stderr)
        sys.exit(1)

    manifest_path = Path(f"manifest-{args.date}.json")
    if manifest_path.exists():
        with open(manifest_path) as f:
            all_files = json.load(f)
        print(f"Loaded {len(all_files)} files from cached manifest")
    else:
        print(f"Listing raw/{args.date} ...")
        all_files = list_date_folder(args.date)
        with open(manifest_path, "w") as f:
            json.dump(all_files, f)
        print(f"Wrote manifest with {len(all_files)} files")

    # Deterministic shard slice
    shard_files = [f for f in all_files if hashlib.md5(f.encode()).digest()[0] % 16 == args.shard]
    print(f"Shard {args.shard}: processing {len(shard_files)} files")

    os.makedirs(args.out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out_dir) / f"shard{args.shard}-{ts}.jsonl"

    dedup = DedupStore()
    written = 0
    skipped_dup = 0

    with open(out_path, "w", buffering=1 << 20) as out_f:
        for file_path in tqdm(shard_files, desc="Shard files"):
            try:
                for pair in project_to_pair(file_path):
                    md5 = slug_for(pair)
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)
                    out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as e:
                print(f"Error processing {file_path}: {e}", file=sys.stderr)
                continue

    print(f"Done. Written {written}, skipped duplicates {skipped_dup} -> {out_path}")

if __name__ == "__main__":
    main()
```

### `lib/dedup.py` (unchanged — reused)

```python
# Existing central md5 dedup store (SQLite)
import sqlite3
from pathlib import Path

class DedupStore:
    def __init__(self, db_path=None):
        if db
