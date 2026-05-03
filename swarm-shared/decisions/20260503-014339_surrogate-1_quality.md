# surrogate-1 / quality

## Final Implementation Plan  
**System**: CDN-bypass manifest-driven ingestion worker (replaces `bin/dataset-enrich.sh`)  
**Repo**: `axentx/surrogate-1-training-pairs`  

---

### Core Design Decisions (resolved)

1. **CDN-only fetch after single manifest snapshot**  
   - Use `list_repo_tree(..., recursive=True)` **once** per run to produce a deterministic file manifest.  
   - All workers fetch via raw CDN URLs (`resolve/main/...`) with **no auth header** to avoid HF API 429.  
   - Manifest is cached as an Actions artifact so workers share the same snapshot and avoid listing repeatedly.

2. **Schema safety and projection-first parsing**  
   - **Never** use `load_dataset(streaming=True)` on heterogeneous repos.  
   - For Parquet: read only `["prompt","response"]` columns via `pyarrow.parquet.read_table(columns=...)`. If that fails, read all columns and project.  
   - For JSON/JSONL: stream line-by-line; require both keys; coerce to string.  
   - Skip files missing either key.

3. **Deterministic sharding + lightweight dedup**  
   - Shard assignment: `hash(slug) % TOTAL_SHARDS == SHARD_ID` (stable across runs).  
   - Per-run in-memory set of row hashes (e.g., `md5(prompt+response)`) to remove obvious duplicates within the same shard.  
   - Global dedup still relies on central SQLite on HF Space (unchanged).

4. **Output and upload**  
   - Write `batches/public-merged/<date>/shard<SHARD_ID>-<HHMMSS>.jsonl`.  
   - Upload via `huggingface_hub` using `HF_TOKEN`.  
   - Commit message includes shard, timestamp, and row count.

5. **Orchestration and failure handling**  
   - GitHub Actions keeps 16-shard matrix.  
   - Each job is independent; failure marks that shard failed.  
   - Add per-job `timeout-minutes` and retry-with-backoff for CDN downloads.

---

### Changes Summary

1. **Create `bin/ingest-worker.py`**  
   - Accepts `SHARD_ID`, `TOTAL_SHARDS`, `--date`.  
   - Optionally accepts `--manifest` path to reuse shared manifest artifact.  
   - Downloads via CDN; projects schema; writes JSONL; uploads.

2. **Update `bin/dataset-enrich.sh`**  
   - Thin wrapper that sets env and invokes `python bin/ingest-worker.py`.  
   - Exits non-zero on failure.

3. **Update `.github/workflows/ingest.yml`**  
   - Add optional pre-step to generate and upload manifest artifact.  
   - Ensure `HF_TOKEN` available.  
   - Set `timeout-minutes` and resource limits.

4. **Add/confirm `requirements.txt`**  
   - `huggingface_hub`, `pyarrow`, `requests`, `tqdm` (optional).

---

### Code

#### `bin/ingest-worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass manifest-driven ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python bin/ingest-worker.py --date 2026-05-03
  SHARD_ID=0 TOTAL_SHARDS=16 python bin/ingest-worker.py --date 2026-05-03 --manifest file-list.json
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

import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, list_repo_tree

REPO_DATASET = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_DATASET}/resolve/main"
HF_TOKEN = os.getenv("HF_TOKEN")
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))

api = HfApi(token=HF_TOKEN)

def deterministic_shard(key: str, n: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % n

def list_files(date_str: str) -> List[str]:
    """List files under date folder (recursive) with fallback layouts."""
    candidates = [
        f"batches/raw/{date_str}",
        f"raw/{date_str}",
        f"batches/{date_str}",
        date_str,
    ]
    for base in candidates:
        try:
            tree = list_repo_tree(repo_id=REPO_DATASET, path=base, recursive=True)
            files = [t.path for t in tree if t.type == "file"]
            if files:
                return files
        except Exception:
            continue
    # Last resort: root recursive
    try:
        tree = list_repo_tree(repo_id=REPO_DATASET, path="", recursive=True)
        files = [t.path for t in tree if t.type == "file" and date_str in t.path]
        if files:
            return files
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
    return []

def load_manifest(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    raise ValueError(f"Unexpected manifest format: {path}")

def safe_read_parquet(path_local: Path) -> Iterable[Dict[str, str]]:
    try:
        tbl = pq.read_table(path_local, columns=["prompt", "response"])
    except Exception:
        try:
            tbl = pq.read_table(path_local)
        except Exception as e:
            print(f"Cannot read parquet {path_local}: {e}", file=sys.stderr)
            return
    df = tbl.to_pandas()
    for _, row in df.iterrows():
        prompt = row.get("prompt")
        response = row.get("response")
        if prompt is None or response is None:
            continue
        yield {"prompt": str(prompt), "response": str(response)}

def safe_read_jsonl(path_local: Path) -> Iterable[Dict[str, str]]:
    with path_local.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt")
            response = obj.get("response")
            if prompt is None or response is None:
                continue
            yield {"prompt": str(prompt), "response": str(response)}

def safe_read_json(path_local: Path) -> Iterable[Dict[str, str]]:
    with path_local.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            return
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]
    else:
        return
    for obj in items:
        prompt = obj.get("prompt")
        response = obj.get("response")
        if prompt is None or response is None:
            continue
        yield {"prompt": str(prompt), "response": str(response)}

def download_cdn_file(repo_path: str, dest: Path, max_retries: int = 3) -> bool:
    url = f"{BASE_CDN}/{repo_path}"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception as e:
            wait = 2 ** attempt
            print(f"Attempt {attempt}/{max_retries} failed for {url}: {e}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return False

def row_hash(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode("
