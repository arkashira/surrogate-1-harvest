# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Single API call from runner (or pre-generated manifest) to list one date folder via `list_repo_tree(..., recursive=False)` → deterministic shard assignment by `hash(slug) % SHARD_TOTAL`
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on fatal failure (GitHub Actions will retry)

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
    HF_TOKEN=hf_xxx \
    python bin/dataset-enrich.py \
      --repo axentx/surrogate-1-training-pairs \
      --date 2026-05-03 \
      --shard 0 \
      --total 16

Behavior:
- Lists REPO_ID/DATE/ via HF API once (or uses provided manifest).
- Assigns files by hash(slug) % TOTAL == ID.
- Downloads via CDN (no auth header) to bypass /api/ rate limits.
- Projects each file to {prompt,response} and dedups via md5 store.
- Outputs batches/public-merged/{DATE}/shard{N}-{TS}.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests

# Local dedup module (shared with HF Space)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("surrogate-ingest")

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"
DEFAULT_REPO = "axentx/surrogate-1-training-pairs"
RETRY_BACKOFF = (1, 2, 4, 8, 16)
MAX_RETRIES = len(RETRY_BACKOFF)


def hf_api_get(url: str, token: Optional[str] = None, **kwargs) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", RETRY_BACKOFF[attempt]))
                log.warning("HF API 429; waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BACKOFF[attempt]
            log.warning("Request error (%s), retry in %ss: %s", exc, wait, exc)
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")


def list_date_files(repo_id: str, date: str, token: Optional[str]) -> List[str]:
    """
    List files under repo_id/date/ (non-recursive).
    Returns relative paths like '2026-05-03/file1.parquet'.
    """
    url = f"{HF_API_BASE}/datasets/{repo_id}/tree"
    params = {"path": date, "recursive": "false"}
    out = hf_api_get(url, token=token, params=params)
    if not isinstance(out, list):
        raise RuntimeError(f"Unexpected tree response: {out}")
    paths = [item.get("path") for item in out if item.get("type") == "file"]
    log.info("Listed %d files under %s/%s", len(paths), repo_id, date)
    return sorted(paths)


def deterministic_shard(paths: List[str], shard_id: int, shard_total: int) -> List[str]:
    assigned = []
    for p in paths:
        slug = Path(p).stem
        digest = hashlib.sha256(slug.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "little") % shard_total
        if bucket == shard_id:
            assigned.append(p)
    log.info("Assigned %d/%d files to shard %d/%d", len(assigned), len(paths), shard_id, shard_total)
    return assigned


def cdn_download(repo_id: str, path: str) -> bytes:
    url = f"{HF_CDN_BASE}/{repo_id}/resolve/main/{path}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", RETRY_BACKOFF[attempt]))
                log.warning("CDN 429; waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BACKOFF[attempt]
            log.warning("CDN download error, retry in %ss: %s", wait, exc)
            time.sleep(wait)
    raise RuntimeError("CDN download exhausted retries")


def project_to_pair(content: bytes, path: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous file to list of {prompt, response}.
    Supports:
    - JSON/JSONL with various key names (prompt/response, instruction/output, etc.)
    - Parquet (via pyarrow) with projection to string columns.
    """
    ext = Path(path).suffix.lower()
    pairs = []

    try:
        if ext == ".parquet":
            table = pq.read_table(pa.BufferReader(content))
            col_names = table.column_names
            prompt_col = None
            response_col = None
            for c in col_names:
                lc = c.lower()
                if "prompt" in lc or "instruction" in lc or "input" in lc:
                    prompt_col = c
                if "response" in lc or "output" in lc or "completion" in lc or "answer" in lc:
                    response_col = c
            if prompt_col is None and len(col_names) >= 1:
                prompt_col = col_names[0]
            if response_col is None and len(col_names) >= 2:
                response_col = col_names[1]
            if prompt_col is None or response_col is None:
                log.warning("Cannot find prompt/response columns in %s; skipping", path)
                return []

            prompts = table.column(prompt_col).to_pylist()
            responses = table.column(response_col).to_pylist()
            for p, r in zip(prompts, responses):
                if p is None or r is None:
                    continue
                pairs.append({"prompt": str(p).strip(), "response": str(r).strip()})

        else:
            text = content.decode("utf-8", errors="replace").strip()
            if not text:
                return []
            if ext == ".jsonl" or (ext == ".json" and "\n" in text):
                lines = [ln.strip() for
