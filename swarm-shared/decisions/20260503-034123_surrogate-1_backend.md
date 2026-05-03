# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-driven, CDN-bypass ingestion**: single `list_repo_tree` call (per date folder) → save `file-list.json`; workers deterministically shard by `hash(slug) % SHARD_TOTAL`; each worker downloads via raw CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to bypass API rate limits
- Projects heterogeneous HF dataset files to `{prompt, response}` only at parse time (avoids pyarrow `CastError` from mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store (same as existing)
- Outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic naming to prevent collisions
- Reuses existing patterns: no `load_dataset(streaming=True)` on mixed-schema repos; HF commit-cap mitigation via date/slug partitioning; Lightning/remote compute stays on Mac orchestration only

---

## Changes

### 1) `bin/dataset-enrich.py` (new)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID          - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE_FOLDER       - dataset subfolder (default: today YYYY-MM-DD)
  HF_TOKEN          - write token (for upload only)
  REPO_ID           - HF dataset repo (default: axentx/surrogate-1-training-pairs)
  MANIFEST_PATH     - local file-list cache (default: file-list.json)
"""
import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests
import pyarrow.parquet as pq
from huggingface_hub import HfApi, hf_hub_download

# ---------- config ----------
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "file-list.json")
API_BASE = f"https://huggingface.co/datasets/{REPO_ID}"

# ---------- dedup ----------
DEDUP_DB_PATH = Path(__file__).parent / "lib" / "dedup.py"
if DEDUP_DB_PATH.exists():
    # Import the existing dedup module dynamically
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", str(DEDUP_DB_PATH))
    dedup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dedup)
    mark_seen = getattr(dedup, "mark_seen", None)
    is_seen = getattr(dedup, "is_seen", None)
else:
    # Fallback in-memory no-op
    _seen = set()
    def mark_seen(key: str) -> bool:
        if key in _seen:
            return False
        _seen.add(key)
        return True
    def is_seen(key: str) -> bool:
        return key in _seen

# ---------- helpers ----------
def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % total

def list_date_folder_files(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    api = HfApi(token=HF_TOKEN or None)
    try:
        tree = api.list_repo_tree(repo_id=REPO_ID, path=date_folder, repo_type="dataset")
    except Exception as e:
        # Fallback: try root if date_folder not found
        if date_folder == "":
            raise
        tree = api.list_repo_tree(repo_id=REPO_ID, path="", repo_type="dataset")
        # Filter by prefix
        items = [str(p.path) for p in tree if str(p.path).startswith(date_folder + "/")]
        return items
    return [str(p.path) for p in tree]

def load_manifest(date_folder: str) -> List[str]:
    cache_path = Path(MANIFEST_PATH)
    if cache_path.exists():
        with open(cache_path) as f:
            data = json.load(f)
            if isinstance(data, dict) and data.get("date_folder") == date_folder:
                return data["files"]
    # regenerate
    files = list_date_folder_files(date_folder)
    cache_path.write_text(json.dumps({"date_folder": date_folder, "files": files}, separators=(",", ":")))
    return files

def cdn_download(url: str, timeout: int = 30) -> bytes:
    """Download via CDN (no auth) with retry on 429/5xx."""
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=timeout, headers={})
            if resp.status_code == 429:
                wait = 360
                print(f"CDN 429, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            if attempt == 4:
                raise
            sleep_t = 2 ** attempt
            print(f"Download retry {attempt+1} after {sleep_t}s: {e}", file=sys.stderr)
            time.sleep(sleep_t)
    raise RuntimeError("Unreachable")

def parse_file_to_pairs(path: str, content: bytes) -> List[Dict[str, str]]:
    """
    Project heterogeneous HF files to {prompt, response}.
    Supports:
      - JSON/JSONL with 'prompt'/'response' or 'instruction'/'output' keys
      - Parquet files (project only required columns)
    Returns a list of valid pairs.
    """
    path_l = path.lower()
    try:
        if path_l.endswith(".parquet"):
            # Use pyarrow to read only minimal columns; tolerate missing cols
            tbl = pq.read_table(pq.ParquetFile(pq.ParquetFile(content).metadata))
            # Try common column names
            prompt_col = next((c for c in tbl.column_names if c in ("prompt", "instruction", "question", "input")), None)
            response_col = next((c for c in tbl.column_names if c in ("response", "output", "answer", "completion")), None)
            if prompt_col is None or response_col is None:
                # fallback: first two string cols
                str_cols = [c for c in tbl.column_names if tbl.schema.field(c).type in (pq.string(), pq.large_string())]
                if len(str_cols) >= 2:
                    prompt_col, response_col = str_cols[0], str_cols[1]
                else:
                    return []
            # Convert to pandas for simplicity (small files in workers)
            df = tbl.to_pandas()
            pairs = []
            for _, row in df.iterrows():
                p = str(row.get(prompt_col, "")).strip()
                r = str(row.get(response_col, "")).strip()
                if p and r:
                    pairs.append({"prompt": p, "response": r})
            return pairs

        # JSON/JSONL
        text = content.decode("utf-8", errors="replace")
        if path_l.endswith(".jsonl"):
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            objs = [json.loads(ln) for ln in lines if ln]
        else:
            objs = json.loads(text)
            if not isinstance(objs, list):
                # Allow single object
                objs = [objs]

        pairs = []
        for obj in objs:
            # Normalize keys
           
