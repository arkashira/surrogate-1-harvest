# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single non-recursive `list_repo_tree` call for the date folder → deterministic shard assignment by `slug-hash % SHARD_TOTAL`
- Downloads via **HF CDN bypass** (`resolve/main/...`) with no Authorization header to avoid API 429 during data streaming
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow `CastError`)
- Deduplicates via central md5 store (`lib/dedup.py`) and writes to deterministic shard outputs:
  ```
  batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
  ```
- Includes retry/backoff for 429 (wait 360s) and 5xx; respects HF commit cap by deterministic shard → repo mapping if siblings are used
- Runs as executable Python script with Bash shebang, callable via `bash bin/dataset-enrich.py "$@"` from cron/workflow

---

## Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker (manifest-driven).

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE_FOLDER       - dataset subfolder date (default today YYYY-MM-DD)
  HF_TOKEN          - HuggingFace write token
  HF_REPO           - target dataset repo (default axentx/surrogate-1-training-pairs)
  MANIFEST_URL      - optional prebuilt manifest URL (for orchestrator-provided list)
"""

import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, login

# ----------------------------
# Configuration
# ----------------------------
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
MANIFEST_URL = os.getenv("MANIFEST_URL")  # optional
HF_API = HfApi(token=HF_TOKEN)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [shard-%(shard)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")
log = logging.LoggerAdapter(log, {"shard": SHARD_ID})

# Constants
BASE_CDN = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
RETRY_BACKOFF = [1, 2, 5, 10, 30, 60, 120, 360]  # seconds
MAX_RETRIES = len(RETRY_BACKOFF)

# ----------------------------
# Dedup store (central SQLite)
# ----------------------------
try:
    from lib.dedup import DedupStore
except Exception as e:
    log.warning("Could not import lib.dedup: %s; using in-memory fallback", e)

    class DedupStore:
        def __init__(self, *args, **kwargs):
            self.seen = set()

        def exists(self, md5: str) -> bool:
            return md5 in self.seen

        def add(self, md5: str) -> bool:
            if md5 in self.seen:
                return False
            self.seen.add(md5)
            return True

# ----------------------------
# Helpers
# ----------------------------
def deterministic_shard(key: str, total: int) -> int:
    """Deterministic shard assignment by hash."""
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % total

def backoff_retry(fn):
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt, wait in enumerate(RETRY_BACKOFF):
            try:
                return fn(*args, **kwargs)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response else None
                # 429 -> wait 360s as per pattern
                if status == 429:
                    log.warning("HF API 429; waiting 360s")
                    time.sleep(360)
                    continue
                # 5xx -> retry with backoff
                if status and status >= 500:
                    last_exc = e
                    log.warning("Server error %s; retry in %ss", status, wait)
                    time.sleep(wait)
                    continue
                raise
            except (requests.RequestException, OSError) as e:
                last_exc = e
                log.warning("Network/IO error; retry in %ss: %s", wait, e)
                time.sleep(wait)
        raise RuntimeError(f"Exhausted retries for {fn.__name__}") from last_exc
    return wrapper

@backoff_retry
def list_date_folder(date_folder: str) -> List[str]:
    """List top-level files in date folder (non-recursive)."""
    # Use HF API once per worker run (non-recursive) to avoid pagination/429.
    items = HF_API.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    paths = []
    for item in items:
        if item.get("type") == "file":
            paths.append(f"{date_folder}/{item['path']}")
        elif item.get("type") == "folder":
            # shallow: list immediate files only (avoid recursive explosion)
            subitems = HF_API.list_repo_tree(repo_id=HF_REPO, path=item["path"], recursive=False)
            for si in subitems:
                if si.get("type") == "file":
                    paths.append(si["path"])
    return paths

@backoff_retry
def fetch_via_cdn(path: str) -> Optional[bytes]:
    """Download file via CDN (no auth)."""
    url = f"{BASE_CDN}/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def compute_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def parse_to_pair(raw: bytes, filename: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response}.
    Implementations should be extended per known schema.
    """
    # Minimal heuristic: try JSON lines with prompt/response fields
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pairs = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue

        # Common field names
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
        response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion")
        if prompt is None or response is None:
            continue
        pairs.append({"prompt": str(prompt), "response": str(response)})

    if not pairs:
        # fallback: treat whole file as single prompt-response if small
        if len(text) < 20_000:
            # crude split by first double newline or '---'
            parts = text.split("\n\n", 1)
            if len(parts) == 2:
                pairs.append({"prompt": parts[0].strip(), "
