# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed manifest** (`manifest-{DATE_FOLDER}.json`) produced by the Mac orchestrator (or generated once per run) containing all file paths under that date folder. Each worker deterministically hashes `path → shard` and only processes its 1/16 slice.
- Downloads files via **HF CDN** (`https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}`) with no Authorization header — bypasses API rate limits entirely.
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError from mixed schemas).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes output to:
  ```
  batches/public-merged/{DATE_FOLDER}/shard{N}-{HHMMSS}.jsonl
  ```
- Commits via HF API (token from `HF_TOKEN`) using deterministic filenames to avoid collisions.
- Adds retry/backoff for CDN 429/5xx and HF commit 429 (wait 360s).

### Why this is the highest-value incremental improvement
- Directly applies the **HF CDN bypass** insight (THE KEY INSIGHT 2026-04-29) to eliminate API rate limits during data load.
- Fixes **pyarrow CastError** by projecting to `{prompt, response}` only at parse time.
- Replaces fragile shell script with robust Python worker (consistent with **opus pr reviewer / active-learning wrapper** lessons: proper shebang, executable, Bash invocation).
- Keeps the 16-shard matrix architecture but makes it deterministic and manifest-driven, enabling zero-API data loading during training.
- Deliverable is a single, focused Python script + small GH Actions tweak — ships in <2h.

---

## Code snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID          - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total shards (default 16)
  DATE_FOLDER       - date folder on dataset repo (default today YYYY-MM-DD)
  HF_TOKEN          - HuggingFace write token
  DATASET_REPO      - dataset repo (default axentx/surrogate-1-training-pairs)
  MANIFEST_URL      - optional precomputed manifest JSON (CDN URL)
"""

import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dataset-enrich")

# Constants
CDN_BASE = "https://huggingface.co/datasets"
API_BASE = "https://huggingface.co/api"
RETRY_WAIT = 360  # seconds for HF 429
MAX_RETRIES = 5

# Env
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
MANIFEST_URL = os.getenv(
    "MANIFEST_URL",
    f"{CDN_BASE}/{DATASET_REPO}/resolve/main/manifest-{DATE_FOLDER}.json",
)

# Paths
HERE = Path(__file__).parent.parent
DEDUP_DB = HERE / "lib" / "dedup.py"  # used as module
OUTPUT_DIR = HERE / "batches" / "public-merged" / DATE_FOLDER
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"


def slug_to_shard(slug: str, total: int) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.md5(slug.encode("utf-8")).hexdigest()
    return int(digest, 16) % total


def load_manifest() -> List[str]:
    """Load manifest JSON from CDN (or fallback to API tree)."""
    # Try CDN manifest first (bypass auth)
    try:
        resp = requests.get(MANIFEST_URL, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                log.info("Loaded manifest from CDN (%d entries)", len(data))
                return data
    except Exception as exc:
        log.warning("Failed to load CDN manifest: %s", exc)

    # Fallback: use HF API tree (single-level recursive=False per folder)
    # This is slower and rate-limited; use only when manifest missing.
    log.info("Falling back to HF API tree (rate-limited)")
    paths = []

    def list_tree(path: str = "") -> List[Dict[str, Any]]:
        url = f"{API_BASE}/datasets/{DATASET_REPO}/tree"
        params = {"path": path, "recursive": False} if path else {"recursive": False}
        r = requests_with_backoff("GET", url, params=params, auth=hf_auth())
        r.raise_for_status()
        return r.json()

    def walk_tree(node_path: str = ""):
        entries = list_tree(node_path)
        for entry in entries:
            p = entry["path"]
            if entry["type"] == "directory":
                walk_tree(p)
            else:
                paths.append(p)

    walk_tree(DATE_FOLDER)
    log.info("Built tree list (%d files)", len(paths))
    return paths


def hf_auth():
    if HF_TOKEN:
        return ("", HF_TOKEN)
    return None


def requests_with_backoff(method: str, url: str, **kwargs):
    retries = 0
    while True:
        try:
            resp = requests.request(method, url, timeout=60, **kwargs)
            if resp.status_code == 429:
                wait = RETRY_WAIT
                log.warning("HF API 429 — waiting %ds", wait)
                time.sleep(wait)
                retries += 1
                if retries > MAX_RETRIES:
                    resp.raise_for_status()
                continue
            if resp.status_code >= 500:
                wait = 2 ** min(retries, 6)
                log.warning("Server error %d — waiting %ds", resp.status_code, wait)
                time.sleep(wait)
                retries += 1
                if retries > MAX_RETRIES:
                    resp.raise_for_status()
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException:
            if retries >= MAX_RETRIES:
                raise
            wait = 2 ** min(retries, 3)
            log.warning("Request failed — retry %d in %ds", retries + 1, wait)
            time.sleep(wait)
            retries += 1


def download_cdn(path: str) -> bytes:
    """Download file via CDN (no auth)."""
    url = f"{CDN_BASE}/{DATASET_REPO}/resolve/main/{path}"
    resp = requests_with_backoff("GET", url)
    return resp.content


def project_to_pair(content: bytes, path: str) -> Dict[str, str]:
    """Project file to {prompt, response}. Supports parquet/jsonl with mixed schemas."""
    # Try parquet first
    try:
        table = pq.read_table(pa.BufferReader(content))
        # Normalize column names
        cols = {
