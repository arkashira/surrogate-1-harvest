# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Pre-lists target folder once via `list_repo_tree(path, recursive=False)` (single API call) → saves `manifest.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero auth/rate-limit during data load
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError)
- Dedups via central md5 store (`lib/dedup.py`)
- Writes output as `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits non-zero on unrecoverable errors; logs summary for GitHub Actions

### Why this is the highest-value incremental improvement
- Directly applies the **HF CDN bypass** insight (eliminates 429s during training)
- Fixes **pyarrow CastError** from mixed schemas by projecting late
- Keeps within <2h scope: swap shell script → Python worker, reuse existing dedup lib, no infra changes
- Enables deterministic sharding + manifest reuse for Lightning training later

---

### Code

#### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage (local/test):
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py

GitHub Actions matrix:
  strategy:
    matrix:
      shard_id: [0,1,2,...,15]
  env:
    SHARD_ID: ${{ matrix.shard_id }}
    SHARD_TOTAL: 16
    DATE: ${{ github.run_started_at || '2026-04-29' }}
"""

import os
import sys
import json
import hashlib
import logging
import io
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
import pyarrow.parquet as pq
import jsonlines
from huggingface_hub import HfApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dataset-enrich")

# ---- config ----
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# ---- paths ----
BASE_DIR = Path(__file__).parent.parent
MANIFEST_PATH = BASE_DIR / "manifest.json"
OUTPUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.utcnow().strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ---- dedup ----
sys.path.insert(0, str(BASE_DIR / "lib"))
try:
    from dedup import is_duplicate, store_hashes
except Exception as e:
    log.warning(f"Could not import dedup: {e}; dedup disabled")
    is_duplicate = lambda h: False  # noqa: E731
    store_hashes = lambda hs: None  # noqa: E731

# ---- helpers ----
def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % SHARD_TOTAL == SHARD_ID

def list_repo_folder(path: str = "") -> List[str]:
    """Single API call: non-recursive tree listing for folder."""
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=path, recursive=False)
        return [item.path for item in tree if item.type == "file"]
    except Exception as e:
        log.error(f"list_repo_tree failed: {e}")
        raise

def save_manifest(paths: List[str]) -> None:
    with open(MANIFEST_PATH, "w") as f:
        json.dump({"repo_id": REPO_ID, "date": DATE, "paths": paths}, f)
    log.info(f"Saved manifest with {len(paths)} files")

def load_manifest() -> List[str]:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            m = json.load(f)
        if m.get("repo_id") == REPO_ID and m.get("date") == DATE:
            log.info(f"Loaded manifest with {len(m['paths'])} files")
            return m["paths"]
    return []

def download_via_cdn(repo_id: str, path: str) -> bytes:
    """CDN bypass: no auth header required for public dataset files."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_parquet_to_pairs(content: bytes) -> List[Dict[str, str]]:
    """Project heterogeneous parquet to {prompt,response} at parse time."""
    table = pq.read_table(io.BytesIO(content))
    pairs = []
    cols = set(table.column_names)

    # Heuristic field names used in surrogate-1 pipelines
    prompt_col = next((c for c in ["prompt", "input", "question", "instruction"] if c in cols), None)
    response_col = next((c for c in ["response", "output", "answer", "completion"] if c in cols), None)

    if prompt_col is None or response_col is None:
        # Fallback: try to find any text-like pair
        text_cols = [c for c in cols if table.schema.field(c).type in ("string", "large_string")]
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        else:
            log.warning(f"No prompt/response columns found in {table.schema.names}; skipping")
            return []

    for i in range(table.num_rows):
        row = {k: table[k][i].as_py() for k in (prompt_col, response_col)}
        if row[prompt_col] is None or row[response_col] is None:
            continue
        pairs.append({"prompt": str(row[prompt_col]).strip(), "response": str(row[response_col]).strip()})
    return pairs

def parse_jsonl_to_pairs(content: bytes) -> List[Dict[str, str]]:
    pairs = []
    for obj in jsonlines.read(io.BytesIO(content)):
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
        response = obj.get("response") or obj.get("output") or obj.get("answer")
        if prompt and response:
            pairs.append({"prompt": str(prompt).strip(), "response": str(response).strip()})
    return pairs

# ---- main ----
def main() -> None:
    log.info(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for {DATE}")

    # 1) manifest: list once, reuse
    paths = load_manifest()
    if not paths:
        paths = list_repo_folder(DATE)  # e.g. "2026-04-29"
        if not paths:
            log.warning(f"No files found for date={DATE}; trying root")
            paths = list_repo_folder("")
        save_manifest(paths)

    if not paths:
        log.error("No files to process")

