# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Pre-lists target folder via **single** `list_repo_tree` call and caches to `manifest-<DATE>.json` to eliminate recursive API calls and 429s
- Uses **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) for zero-auth, high-rate downloads during ingest
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes deterministic output: `batches/public-merged/<DATE>/shard<SHARD_ID>-<HHMMSS>.jsonl`
- Uses deterministic hash-slug → shard assignment to avoid collisions across runners
- Exits cleanly on 429/5xx with jittered backoff and retries
- Includes shebang, `chmod +x`, and Bash-safe invocation for cron

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingest worker (shard-level).
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID            (required) 0..15
  SHARD_TOTAL         (default 16)
  DATE                (required) YYYY-MM-DD folder under dataset
  HF_TOKEN            (required) write token for repo
  REPO_ID             (default axentx/surrogate-1-training-pairs)
  MANIFEST_PATH       (optional) path to prebuilt manifest JSON
"""

import os
import sys
import json
import hashlib
import time
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download

# Project local
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# Defaults
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
HF_TOKEN = os.getenv("HF_TOKEN")
DATE = os.getenv("DATE")
SHARD_ID = os.getenv("SHARD_ID")

if not all([HF_TOKEN, DATE, SHARD_ID]):
    log.error("Missing required env: HF_TOKEN, DATE, SHARD_ID")
    sys.exit(1)

try:
    SHARD_ID = int(SHARD_ID)
    if not (0 <= SHARD_ID < SHARD_TOTAL):
        raise ValueError
except ValueError:
    log.error("SHARD_ID must be int in [0, SHARD_TOTAL-1]")
    sys.exit(1)

MANIFEST_PATH = os.getenv(
    "MANIFEST_PATH",
    f"manifest-{DATE}.json"
)

API = HfApi(token=HF_TOKEN)
DEDUP = DedupStore()
SESSION = requests.Session()
# CDN downloads do not require auth header; keep session clean.

MAX_RETRIES = 5
BACKOFF_BASE = 1.0  # seconds
JITTER = 0.3

def jittered_backoff(attempt: int) -> float:
    return BACKOFF_BASE * (2 ** attempt) * (1 + JITTER * (random.random() - 0.5))

def request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    for attempt in range(MAX_RETRIES):
        try:
            resp = SESSION.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                wait = jittered_backoff(attempt)
                log.warning("HTTP %d on %s; retry %d/%d after %.1fs",
                            resp.status_code, url, attempt + 1, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.RequestException, requests.Timeout) as exc:
            if attempt == MAX_RETRIES - 1:
                log.exception("Request failed after retries: %s %s", method, url)
                raise
            wait = jittered_backoff(attempt)
            log.warning("Request error on %s; retry %d/%d after %.1fs: %s",
                        url, attempt + 1, MAX_RETRIES, wait, exc)
            time.sleep(wait)
    raise RuntimeError(f"Exhausted retries for {method} {url}")

def list_files_for_date(date: str) -> List[str]:
    """Single API call: non-recursive tree for date folder."""
    log.info("Listing repo tree for date=%s repo=%s", date, REPO_ID)
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=date, repo_type="dataset")
    except Exception as exc:
        log.exception("Failed to list repo tree")
        raise
    files = [item.path for item in tree if item.type == "file"]
    log.info("Found %d files in %s", len(files), date)
    return sorted(files)

def build_or_load_manifest(date: str) -> List[str]:
    """Use existing manifest if present; otherwise build and save."""
    manifest_path = Path(MANIFEST_PATH)
    if manifest_path.exists():
        log.info("Using existing manifest: %s", manifest_path)
        return json.loads(manifest_path.read_text())

    files = list_files_for_date(date)
    manifest_path.write_text(json.dumps(files, indent=2))
    log.info("Saved manifest: %s", manifest_path)
    return files

def shard_filter(path: str, shard_id: int, shard_total: int) -> bool:
    """Deterministic shard assignment by path hash."""
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return (h % shard_total) == shard_id

def cdn_download_url(repo_id: str, path: str) -> str:
    """CDN bypass URL (no auth)."""
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

def parse_file_to_pairs(content: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Supports common patterns seen in surrogate-1:
      - JSON/JSONL with 'prompt'/'response' or 'instruction'/'output'
      - Parquet projected via hf_hub_download + pyarrow (fallback)
    """
    import json as jsonlib

    # Try JSON/JSONL text first
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Likely binary (parquet); fallback to hf_hub_download + pyarrow
        return parse_parquet_fallback(repo_id=REPO_ID, path=filename)

    # JSONL
    pairs = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        try:
            obj = jsonlib.loads(line)
        except jsonlib.JSONDecodeError:
            continue
        prompt, response = normalize_pair(obj)
        if prompt is not None and response is not None:
            pairs.append({"prompt": prompt, "response": response})
    return pairs

def parse_parquet_fallback(repo_id: str, path: str) -> List[Dict[str, str]]:
    """Download parquet and project columns; avoids streaming mixed schemas."""
    try:
        import pyarrow.parquet as pq
        local_path = hf_hub_download(repo_id=repo_id, filename=path, repo_type="dataset")
        table = pq.read_table(local_path, columns=["prompt", "response"] if "prompt" in pq.read_schema(local_path
