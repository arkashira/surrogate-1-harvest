# surrogate-1 / discovery

### Final Implementation Plan  
**Goal:** Replace `bin/dataset-enrich.sh` with a robust, manifest-driven, CDN-bypass ingestion worker (`bin/dataset-enrich.py`) that is deterministic, shard-aware, rate-limit-safe, and deployable within 2 hours.

---

### 1. Script Interface and Environment  
Use environment variables (CI-friendly) **and** CLI flags (local/dev-friendly). Resolve contradictions by prioritizing CI usage but supporting both.

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker.
Usage (CI):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2025-01-01 HF_TOKEN=hf_xxx \
    python bin/dataset-enrich.py
Usage (local):
  python bin/dataset-enrich.py --shard_id 0 --shard_total 16 \
    --date 2025-01-01 --hf_token hf_xxx --repo_id axentx/surrogate-1-training-pairs
"""
import argparse
import os
import sys
import json
import hashlib
import logging
from pathlib import Path

import requests
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfApi, hf_hub_download, Repository

# ----------------------------
# Configuration resolution
# ----------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Manifest-driven CDN-bypass ingestion worker")
    parser.add_argument("--shard_id", type=int, help="Shard index (0-based)")
    parser.add_argument("--shard_total", type=int, default=16, help="Total shards")
    parser.add_argument("--date", help="Ingestion date (YYYY-MM-DD)")
    parser.add_argument("--hf_token", help="Hugging Face token (write)")
    parser.add_argument("--repo_id", default="axentx/surrogate-1-training-pairs", help="HF dataset repo")
    parser.add_argument("--work_dir", default="/tmp/hf-ingest", help="Working directory")
    parser.add_argument("--manifest_path", help="Optional local manifest JSON for testing")
    return parser.parse_args()

args = parse_args()

# Environment overrides CLI when present (CI behavior)
SHARD_ID = int(os.getenv("SHARD_ID", args.shard_id if args.shard_id is not None else 0))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", args.shard_total))
DATE = os.getenv("DATE", args.date)
HF_TOKEN = os.getenv("HF_TOKEN", args.hf_token)
REPO_ID = os.getenv("REPO_ID", args.repo_id)
WORK_DIR = Path(os.getenv("WORK_DIR", args.work_dir))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", args.manifest_path)

if not DATE:
    print("ERROR: DATE is required.", file=sys.stderr)
    sys.exit(1)
if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required.", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
WORK_DIR.mkdir(parents=True, exist_ok=True)
```

---

### 2. Manifest Fetching (CDN-bypass, Rate-Limit Safe)  
Use the Hugging Face **CDN raw file listing via `resolve/main`** for the date prefix. If unavailable, fall back to `huggingface_hub` list_repo_files (slower, API-limited). Prefer CDN.

```python
def fetch_manifest():
    """Return list of parquet filenames for DATE from repo."""
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        logging.info("Using local manifest: %s", MANIFEST_PATH)
        return json.loads(Path(MANIFEST_PATH).read_text())

    # CDN directory listing attempt (common pattern for HF datasets with tree output)
    listing_url = f"https://huggingface.co/{REPO_ID}/tree/main/{DATE}"
    resp = requests.get(listing_url, timeout=30)
    if resp.status_code == 200:
        # Heuristic: parse JSON if returned; otherwise try to parse HTML listing (less reliable)
        try:
            data = resp.json()
            if isinstance(data, list):
                files = [item["path"] for item in data if item.get("type") == "file" and item["path"].endswith(".parquet")]
                if files:
                    logging.info("Fetched manifest via CDN tree JSON: %d files", len(files))
                    return files
        except Exception:
            pass

    # Fallback: use huggingface_hub to list repo files (API-limited)
    try:
        api = HfApi(token=HF_TOKEN)
        files = [f.rfilename for f in api.list_repo_files(repo_id=REPO_ID, repo_type="dataset") if f.rfilename.startswith(f"{DATE}/") and f.rfilename.endswith(".parquet")]
        logging.info("Fetched manifest via HF API: %d files", len(files))
        return files
    except Exception as e:
        logging.error("Failed to fetch manifest: %s", e)
        sys.exit(1)

manifest_files = fetch_manifest()
if not manifest_files:
    logging.warning("No parquet files found for date %s", DATE)
    sys.exit(0)
```

---

### 3. Shard Assignment and Deterministic Routing  
Distribute files across shards by filename hash to ensure idempotency and avoid overlap.

```python
def shard_for_file(filename):
    """Deterministic shard assignment by filename hash."""
    h = int(hashlib.sha256(filename.encode()).hexdigest(), 16)
    return h % SHARD_TOTAL

my_files = [f for f in manifest_files if shard_for_file(f) == SHARD_ID]
logging.info("Shard %d/%d assigned %d files", SHARD_ID, SHARD_TOTAL, len(my_files))
if not my_files:
    logging.info("No files assigned to this shard. Exiting.")
    sys.exit(0)
```

---

### 4. CDN-Bypass Download and Processing  
Download directly via CDN `resolve/main/...` URLs. Stream to disk to avoid OOM. Process with pyarrow.

```python
def cdn_download(url, dest):
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

def load_parquet_safe(path):
    try:
        return pq.read_table(path)
    except Exception as e:
        logging.error("Failed to read parquet %s: %s", path, e)
        return None

# Local repo clone for uploads
repo_path = WORK_DIR / "repo"
if repo_path.exists():
    repo = Repository(local_dir=str(repo_path), repo_id=REPO_ID, token=HF_TOKEN, clone_from=REPO_ID)
else:
    repo = Repository(local_dir=str(repo_path), repo_id=REPO_ID, token=HF_TOKEN)
repo.git_pull()
```

---

### 5. Deduplication (lib/dedup.py)  
Use a robust, schema-agnostic row hash dedup. Candidate 2’s example was too naive (column-wise). We deduplicate across all columns.

`lib/dedup.py`:
```python
import pyarrow as pa
import hashlib

def row_hash(batch):
    # Create deterministic row hash across all columns
    arrays = [batch.column(i) for i in range(batch.num_columns)]
    hashes = []
    for i in range(batch.num_rows):
        row_vals = tuple(arr[i].as_py() for arr in arrays)
        h = hashlib.sha256(json.dumps(row_vals, sort_keys=True, default=str).encode()).hexdigest()
        hashes.append(h)
    return pa.array(hashes)

def dedup(table):
    if table.num_rows == 0:
        return table
    df = table.to_pandas()
    df.drop_duplicates(inplace=True)
    return pa.Table.from_pandas(df, preserve_index=False)
```

In `dataset-enrich.py`, import and use:
```python
from lib.dedup import dedup
```

---

### 6. Processing Loop and Upload  
For each assigned file: download → process → dedup → append to shard output → upload shard file.

```python
shard_rows = []

for rel_path in my_files:
    filename = Path(rel_path).name

