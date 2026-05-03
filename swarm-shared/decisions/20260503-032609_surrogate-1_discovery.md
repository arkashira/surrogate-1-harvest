# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **one API call** per run to list the date folder → saves `file-list.json`
- Downloads **only assigned shard** via **HF CDN direct URLs** (`resolve/main/...`) — zero auth, bypasses `/api/` 429
- Projects heterogeneous files to `{prompt, response}` at parse time (avoids pyarrow `CastError`)
- Dedups via central `lib/dedup.py` md5 store (SQLite)
- Outputs to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Exits non-zero on unrecoverable errors; logs structured JSON for Actions

---

## File: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID          - required; 0..SHARD_TOTAL-1
  SHARD_TOTAL       - optional; default 16
  DATE_FOLDER       - optional; default today YYYY-MM-DD
  HF_REPO           - optional; default axentx/surrogate-1-training-pairs
  HF_TOKEN          - optional; write token for upload (not used for CDN reads)
  UPLOAD_BATCH_SIZE - optional; default 5000
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx  # prefer httpx for streaming + retries
import pyarrow as pa
import pyarrow.parquet as pq

# Add repo root to path for lib imports
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.dedup import DedupStore  # noqa: E402

# ---- constants ----
DEFAULT_SHARD_TOTAL = 16
DEFAULT_REPO = "axentx/surrogate-1-training-pairs"
CDN_BASE = "https://huggingface.co/datasets"
UPLOAD_BATCH_SIZE = int(os.getenv("UPLOAD_BATCH_SIZE", "5000"))
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 5
RETRY_BACKOFF = 5.0

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# ---- utils ----
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def deterministic_shard(key: str, total: int) -> int:
    """Map key to shard by md5 hash."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % total

def hf_api_get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", RETRY_BACKOFF))
                log.warning("rate-limited (429); waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500 and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning("server error %s; retry %s/%s in %ss", e, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {url}")

# ---- file listing ----
def list_date_folder(
    repo: str,
    date_folder: str,
    client: httpx.Client,
) -> List[str]:
    """
    List files in repo under date_folder (non-recursive).
    Uses HF API tree endpoint. Returns relative paths.
    """
    # tree endpoint: /api/datasets/{repo}/tree/{revision}/{path}
    url = f"https://huggingface.co/api/datasets/{repo}/tree/main/{date_folder}"
    resp = hf_api_get(client, url)
    items = resp.json()
    if not isinstance(items, list):
        raise ValueError(f"Unexpected tree response: {items}")
    paths = [it["path"] for it in items if it.get("type") == "file"]
    log.info("listed %d files in %s/%s", len(paths), repo, date_folder)
    return sorted(paths)

# ---- CDN download + projection ----
def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_BASE}/{repo}/resolve/main/{path}"

def stream_cdn_lines(client: httpx.Client, url: str) -> Iterable[bytes]:
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes(chunk_size=8192):
            if chunk:
                yield chunk

def parse_file_to_pairs(content: bytes, path: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous file to [{prompt, response}] pairs.
    Supports:
      - JSONL (one object per line) with any schema -> extract prompt/response keys
      - Parquet -> read and project columns
      - JSON (single array or object)
    Unknown files -> empty list.
    """
    ext = Path(path).suffix.lower()
    pairs = []

    try:
        if ext == ".parquet":
            table = pq.read_table(pa.BufferReader(content))
            cols = table.column_names
            prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
            response_col = next((c for c in cols if "response" in c.lower() or "completion" in c.lower()), None)
            if prompt_col and response_col:
                prompts = table.column(prompt_col).to_pylist()
                responses = table.column(response_col).to_pylist()
                for p, r in zip(prompts, responses):
                    if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                        pairs.append({"prompt": p.strip(), "response": r.strip()})
            return pairs

        # Try JSONL first
        text = content.decode("utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion")
            if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                pairs.append({"prompt": prompt.strip(), "response": response.strip()})
        if pairs:
            return pairs

        # Try single JSON object/array
        obj = json.loads(text)
        if isinstance(obj, dict):
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion")
            if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                pairs.append({"prompt": prompt.strip(), "response": response.strip()})
        elif isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                prompt = item.get("prompt") or item.get("input") or item.get("question")
                response = item.get("response") or item.get("output") or item.get("answer")
