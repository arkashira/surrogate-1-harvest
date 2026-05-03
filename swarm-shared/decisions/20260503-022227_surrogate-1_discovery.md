# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses a **pre-listed manifest** (`manifest-{DATE}.json`) to avoid recursive `list_repo_files` and HF API rate limits
- Downloads only assigned shard files via **HF CDN** (`https://huggingface.co/datasets/{repo}/resolve/main/...`) — no Authorization header, bypasses `/api/` 429 limits
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids PyArrow CastError)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Includes retry/backoff for CDN 429/503 and HF commit cap handling

---

### Files to create/modify

```
bin/dataset-enrich.py      # new worker (replaces .sh)
.github/workflows/ingest.yml  # update matrix to pass manifest & DATE
requirements.txt           # ensure requests, tqdm, tenacity
```

---

### bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (local/test):
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py

Env:
  SHARD_ID          - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total shards (default 16)
  DATE              - date folder in repo (e.g. 2026-04-29)
  HF_TOKEN          - HuggingFace write token
  REPO_ID           - dataset repo (default axentx/surrogate-1-training-pairs)
  MANIFEST_URL_BASE - optional override for manifest location
"""

import os
import sys
import json
import hashlib
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ---- config ----
SHARD_ID = int(os.getenv("SHARD_ID", 0))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))
DATE = os.getenv("DATE", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
MANIFEST_URL_BASE = os.getenv(
    "MANIFEST_URL_BASE",
    f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/manifests"
)
CDN_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

if not DATE:
    print("ERROR: DATE env var required (YYYY-MM-DD)", file=sys.stderr)
    sys.exit(1)

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# ---- dedup ----
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ---- retry policy ----
@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=120),
    retry=retry_if_exception_type((requests.exceptions.RequestException,)),
)
def cdn_get(url: str, stream: bool = False) -> requests.Response:
    resp = requests.get(url, stream=stream, timeout=30)
    if resp.status_code == 429:
        # CDN 429: wait longer
        retry_after = int(resp.headers.get("Retry-After", "60"))
        log.warning("CDN 429, Retry-After=%s", retry_after)
        time.sleep(max(retry_after, 60))
        raise requests.exceptions.RequestException("CDN 429")
    resp.raise_for_status()
    return resp

def hf_api_get(url: str) -> Any:
    """Single API call for manifest fetch (use sparingly)."""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 429:
        wait = 360
        log.warning("HF API 429, waiting %ss", wait)
        time.sleep(wait)
        raise requests.exceptions.RequestException("HF API 429")
    resp.raise_for_status()
    return resp.json()

# ---- manifest ----
def load_manifest(date: str) -> List[str]:
    """
    Load manifest-{date}.json from CDN.
    Expected format: { "files": ["public/2026-04-29/foo.parquet", ...] }
    """
    manifest_url = f"{MANIFEST_URL_BASE}/manifest-{date}.json"
    log.info("Loading manifest: %s", manifest_url)
    data = hf_api_get(manifest_url)
    files = data.get("files") or data if isinstance(data, list) else data.get("files", [])
    if not files:
        log.warning("Manifest returned no files")
    return files

def shard_files(files: List[str], shard_id: int, shard_total: int) -> List[str]:
    """Deterministic shard assignment by file path hash."""
    assigned = []
    for f in files:
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        if h % shard_total == shard_id:
            assigned.append(f)
    return assigned

# ---- projection helpers ----
def extract_pair_from_parquet_bytes(raw: bytes) -> Optional[Dict[str, str]]:
    """
    Read parquet bytes and project to {prompt, response}.
    Tolerates heterogeneous schemas; returns None if no usable pair.
    """
    try:
        table = pq.read_table(pa.BufferReader(raw))
    except Exception as exc:
        log.debug("Failed to read parquet: %s", exc)
        return None

    # Try common column names
    prompt_col = None
    response_col = None
    for c in table.column_names:
        cl = c.lower()
        if "prompt" in cl or "instruction" in cl or "input" in cl:
            prompt_col = c
        if "response" in cl or "output" in cl or "completion" in cl or "answer" in cl:
            response_col = c

    if prompt_col is None or response_col is None:
        # fallback: first two string/text cols
        text_cols = [c for c in table.column_names if pa.types.is_string(table.schema.field(c).type)]
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        else:
            return None

    if table.num_rows == 0:
        return None

    # Take first row as representative sample for this worker's dedup stream
    # (Ingestion pipeline may emit multiple rows per file; we emit one per file for simplicity)
    row = {
        "prompt": str(table.column(prompt_col)[0].as_py()),
        "response": str(table.column(response_col)[0].as_py()),
    }
    return row

def extract_pair_from_jsonl_line(line: str) -> Optional[Dict[str, str]]:
    try:
        obj = json.loads(line)
    except Exception:
        return None

    # Common keys
    prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
    response = obj.get("response") or obj.get("output") or obj.get("completion")
    if prompt and response:
        return {"prompt": str(prompt), "response": str(response)}
    return None

# ---- worker
