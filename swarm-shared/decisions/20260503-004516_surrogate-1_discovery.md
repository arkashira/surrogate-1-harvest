# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during training). This implements the CDN bypass pattern and prevents 429 rate limits during Lightning training.

### Steps (1h 30m total)

1. **Create tools/snapshot_manifest.py** (20m)  
   - Single `list_repo_tree` call for `public-merged/<date>/` (non-recursive)  
   - Deterministic sort for reproducibility  
   - Build CDN URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  
   - Output `file_manifest.json` with `{date, repo, files: [{path, cdn_url, size}], generated_at}`  
   - Accept CLI args: `--repo`, `--date`, `--out`

2. **Create training/data_loader_cdn.py** (30m)  
   - Read `file_manifest.json`  
   - Use `requests.get(cdn_url, stream=True)` with retry/backoff  
   - Parse parquet → project `{prompt, response}` only (tolerate `input/output/completion` variants)  
   - Yield dicts; optional local per-file cache

3. **Update training/train.py** (20m)  
   - Import CDN loader; replace `load_dataset` calls  
   - Add CLI flag `--manifest` to point to manifest  
   - Ensure zero `huggingface_hub` API usage during training loop

4. **Add requirements-dev.txt** (5m)  
   - Include `requests`, `tqdm`, `pyarrow`, `pandas`

5. **Smoke test on Mac** (15m)  
   - Run snapshot script → verify manifest  
   - Run loader on 1 file → verify projection  
   - Dry-run training step (no actual epochs)

---

## Code Snippets

### tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for a date partition.
Usage:
    python snapshot_manifest.py \
        --repo axentx/surrogate-1-training-pairs \
        --date 2026-04-29 \
        --out file_manifest.json
"""
import argparse
import json
import sys
from datetime import datetime

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for a date partition")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-1-training-pairs)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    folder_path = f"batches/public-merged/{args.date}"
    try:
        tree = api.list_repo_tree(repo_id=args.repo, path=folder_path, recursive=False)
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for item in sorted(tree, key=lambda x: x.path):
        if item.type != "file":
            continue
        path = f"{folder_path}/{item.path.split('/')[-1]}"
        files.append({
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=args.repo, path=path),
            "size": getattr(item, "size", None),
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

### training/data_loader_cdn.py
```python
import json
import time
from pathlib import Path
from typing import Iterator, Dict, Any

import pandas as pd
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

RETRY_BACKOFF = (1, 2, 4, 8)  # seconds

def _fetch_with_retry(url: str, max_retries: int = 4) -> bytes:
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            time.sleep(RETRY_BACKOFF[attempt])
    raise RuntimeError("unreachable")

def cdn_parquet_iterator(manifest_path: str) -> Iterator[Dict[str, Any]]:
    """Yield {prompt, response} rows from CDN parquet files listed in manifest."""
    manifest = json.loads(Path(manifest_path).read_text())
    for entry in tqdm(manifest["files"], desc="CDN files"):
        url = entry["cdn_url"]
        data = _fetch_with_retry(url)
        table = pq.read_table(pq.ParquetFile(pq.BufferReader(data)))
        df = table.to_pandas()
        # Project to canonical schema; tolerate variations
        for _, row in df.iterrows():
            prompt = row.get("prompt") or row.get("input") or row.get("text")
            response = row.get("response") or row.get("output") or row.get("completion")
            if prompt is None or response is None:
                continue
            yield {"prompt": str(prompt), "response": str(response)}
```

### training/train.py (partial)
```python
import argparse
from pathlib import Path

from data_loader_cdn import cdn_parquet_iterator

def train_step(batch):
    # Your training logic here
    pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    # CDN-only data loader — zero HF API calls during training
    data_iter = cdn_parquet_iterator(args.manifest)

    for epoch in range(args.epochs):
        batch = []
        for sample in data_iter:
            batch.append(sample)
            if len(batch) == 32:
                train_step(batch)
                batch = []
        if batch:
            train_step(batch)

if __name__ == "__main__":
    main()
```

### requirements-dev.txt
```
requests>=2.31
tqdm>=4.66
pyarrow>=14
pandas>=2.0
huggingface_hub>=0.22
```

---

## Smoke Test Commands
```bash
# 1) Generate manifest
python tools/snapshot_manifest.py \
  --repo axentx/surrogate-1-training-pairs \
  --date 2026-04-29 \
  --out file_manifest.json

# 2) Verify manifest
cat file_manifest.json | jq '.files | length'

# 3) Dry-run loader (first file only)
python -c "
from training.data_loader_cdn import cdn_parquet_iterator
it = cdn_parquet_iterator('file_manifest.json')
for i, row in enumerate(it):
    print(row)
    if i >= 2: break
"

# 4) Dry-run training (no epochs)
python training/train.py --manifest file_manifest.json --epochs 0
```
