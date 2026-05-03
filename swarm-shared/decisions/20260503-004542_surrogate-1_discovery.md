# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during training). This directly applies the HF CDN bypass pattern and eliminates rate-limit/429 risk during long training runs.

### Steps (1h 30m total)

1. **Create tools/snapshot_manifest.py** (20m)  
   - Single API call: `list_repo_tree(path=date_partition, recursive=False)`  
   - Output: `file_manifest.json` → `{"date": "YYYY-MM-DD", "files": [{"path": "...", "cdn_url": "https://huggingface.co/datasets/.../resolve/main/...", "size": int}]}`  
   - Deterministic sort for reproducibility.

2. **Create training/data_loader.py** (30m)  
   - Read `file_manifest.json` at startup.  
   - Use `requests.get(cdn_url, stream=True)` with retry/backoff to stream parquet files.  
   - Use `pyarrow.parquet.ParquetFile` on stream to project only `{prompt, response}` columns.  
   - No `load_dataset` or `hf_api` calls during training loop.

3. **Update bin/dataset-enrich.sh** (10m)  
   - Optional: add a dry-run flag to invoke `snapshot_manifest.py` and validate CDN URLs before heavy ingestion.

4. **Add lightweight CLI entrypoint** (10m)  
   - `python -m tools.snapshot_manifest --date 2026-04-29 --repo axentx/surrogate-1-training-pairs --out manifest.json`

5. **Validation & smoke test** (20m)  
   - Run manifest snapshot on Mac.  
   - Run data loader in a small script that iterates 2 files and prints record counts.  
   - Confirm zero HF API calls via logging.

---

### Code Snippets

#### tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Snapshot one date-partition of a HuggingFace dataset into a manifest
with CDN URLs (bypasses HF API rate limits during training).

Usage:
    python -m tools.snapshot_manifest \
        --repo axentx/surrogate-1-training-pairs \
        --date 2026-04-29 \
        --out file_manifest.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def snapshot_partition(repo: str, date: str, out_path: str):
    """
    date: YYYY-MM-DD  (must match folder name in repo)
    """
    api = HfApi()
    prefix = f"{date}/"
    try:
        tree = api.list_repo_tree(repo=repo, path=prefix, recursive=False)
    except Exception as exc:
        print(f"HF API error listing {repo}@{prefix}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for item in sorted(tree, key=lambda x: x.path):
        if item.type != "file":
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=item.path)
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
        })

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out}")

def main():
    parser = argparse.ArgumentParser(description="Create CDN manifest for a date partition.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (folder name in dataset)")
    parser.add_argument("--out", default="file_manifest.json")
    args = parser.parse_args()
    snapshot_partition(repo=args.repo, date=args.date, out_path=args.out)

if __name__ == "__main__":
    main()
```

#### training/data_loader.py
```python
import io
import json
import time
from pathlib import Path
from typing import Iterator, Dict, Any

import numpy as np
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter, Retry

MANIFEST_PATH = Path(__file__).parent.parent / "file_manifest.json"

def _make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session

def load_manifest(manifest_path: Path = MANIFEST_PATH) -> Dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text())

def stream_parquet_cdn(url: str, session: requests.Session, columns=("prompt", "response")) -> pq.Table:
    """Stream a parquet file from CDN and return projected table."""
    with session.get(url, stream=True) as resp:
        resp.raise_for_status()
        buffer = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=8192):
            buffer.write(chunk)
        buffer.seek(0)
        pf = pq.ParquetFile(buffer)
        # Project only required columns to reduce memory
        return pf.read(columns=columns)

def iter_dataset(manifest_path: Path = MANIFEST_PATH) -> Iterator[Dict[str, str]]:
    manifest = load_manifest(manifest_path)
    session = _make_session()

    for entry in manifest["files"]:
        url = entry["cdn_url"]
        try:
            table = stream_parquet_cdn(url, session, columns=("prompt", "response"))
        except Exception as exc:
            print(f"Skipping {url} due to error: {exc}")
            continue

        df = table.to_pandas()
        for _, row in df.iterrows():
            prompt = str(row.get("prompt", ""))
            response = str(row.get("response", ""))
            if prompt.strip() and response.strip():
                yield {"prompt": prompt, "response": response}

def dataset_stats(manifest_path: Path = MANIFEST_PATH):
    manifest = load_manifest(manifest_path)
    session = _make_session()
    total_files = len(manifest["files"])
    total_rows = 0
    total_bytes = 0

    for entry in manifest["files"]:
        url = entry["cdn_url"]
        try:
            table = stream_parquet_cdn(url, session, columns=("prompt", "response"))
            total_rows += table.num_rows
            total_bytes += table.nbytes
        except Exception as exc:
            print(f"Error processing {url}: {exc}")

    print(f"Files: {total_files}")
    print(f"Rows:  {total_rows}")
    print(f"Bytes (projected): {total_bytes}")

if __name__ == "__main__":
    # Quick smoke test
    dataset_stats()
```

#### Optional: bin/dataset-enrich.sh (add dry-run validation)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
MANIFEST="file_manifest.json"

# Generate manifest (Mac orchestration step)
if [[ "${DRY_RUN:-0}" == "1" ]]; then
   
