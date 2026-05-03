# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Changes

1. **Add `bin/worker.py`** — deterministic shard worker that:
   - Uses a single `list_repo_tree` call (per date folder) from the orchestrator, embeds file list in a `manifest.json`
   - Downloads only assigned shard files via HF CDN (`resolve/main/...`) with no Authorization header (bypasses `/api/` rate limits)
   - Projects each file to `{prompt, response}` at parse time (avoids `pyarrow.CastError` from heterogeneous schemas)
   - Produces `shard-<N>-<HHMMSS>.parquet` in `batches/public-merged/<date>/`
   - Writes lightweight JSONL for compatibility with existing pipeline
   - Uses deterministic sharding via `hash(path) % shard_count` for stable, reproducible assignments

2. **Update `bin/dataset-enrich.sh`** — thin wrapper that:
   - Accepts `SHARD_ID` and `MANIFEST_FILE` (or falls back to generating manifest once per runner)
   - Invokes `python3 bin/worker.py` with proper args
   - Keeps executable + Bash shebang for cron/workflow compatibility

3. **Workflow tweak** — pass `date` and optional precomputed manifest (or let each runner compute once and reuse) to avoid repeated `list_repo_tree` calls across shards.

4. **Dedup integration** — reuse existing `lib/dedup.py` for cross-run/source dedup (unchanged).

---

### Code snippets

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public dataset ingestion.

Usage:
  SHARD_ID=0 SHARD_COUNT=16 python bin/worker.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out-dir batches/public-merged/2026-05-03 \
    --manifest manifest.json

Behavior:
- If --manifest is provided, uses it (list of {"path": ..., "size": ...}).
- Otherwise calls list_repo_tree once for the date folder (non-recursive).
- Downloads assigned files via HF CDN (no auth header) to bypass /api/ rate limits.
- Projects each file to {prompt, response} at parse time to avoid mixed-schema CastError.
- Outputs shard-N-HHMMSS.parquet + .jsonl for compatibility.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store (unchanged interface)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
HF_RETRY_WAIT = 360  # seconds after 429

# Schema projection: keep only these fields; everything else dropped.
TARGET_FIELDS = {"prompt", "response"}

def utcnow_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def list_files_for_date(repo: str, date: str) -> List[Dict[str, Any]]:
    """
    Single API call: list top-level files in date folder (non-recursive).
    Avoids recursive list_repo_files to stay within HF API limits.
    """
    try:
        tree = API.list_repo_tree(repo=repo, path=date, recursive=False)
    except Exception as exc:
        # If rate-limited, wait and retry once
        print(f"list_repo_tree failed: {exc}", file=sys.stderr)
        time.sleep(HF_RETRY_WAIT)
        tree = API.list_repo_tree(repo=repo, path=date, recursive=False)

    files = []
    for entry in tree:
        if entry.type == "file":
            files.append({"path": f"{date}/{entry.path}", "size": getattr(entry, "size", 0)})
    return files

def load_manifest(manifest_path: str) -> List[Dict[str, Any]]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    return data

def project_to_pair(obj: Any) -> Dict[str, str]:
    """
    Project arbitrary parsed object to {prompt, response}.
    Supports dict-like and common HF dataset row shapes.
    """
    if isinstance(obj, dict):
        src = obj
    elif hasattr(obj, "__dict__"):
        src = obj.__dict__
    else:
        src = {}

    prompt = src.get("prompt") or src.get("input") or src.get("question") or ""
    response = src.get("response") or src.get("output") or src.get("answer") or ""
    # Ensure strings
    return {"prompt": str(prompt) if prompt is not None else "", "response": str(response) if response is not None else ""}

def download_via_cdn(url: str, timeout: int = 30) -> bytes:
    """Download via CDN without Authorization header (bypasses /api/ rate limits)."""
    resp = requests.get(url, timeout=timeout, headers={})
    if resp.status_code == 429:
        print(f"CDN 429 on {url}, waiting {HF_RETRY_WAIT}s", file=sys.stderr)
        time.sleep(HF_RETRY_WAIT)
        resp = requests.get(url, timeout=timeout, headers={})
    resp.raise_for_status()
    return resp.content

def parse_file_to_rows(path: str, repo: str, dedup: DedupStore) -> List[Dict[str, str]]:
    """
    Download and parse a single repo file, project to pairs, and dedup by content hash.
    """
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    raw = download_via_cdn(url)

    # Try parquet first (common for HF datasets), fallback to json/jsonl
    rows: List[Dict[str, str]] = []
    try:
        table = pq.read_table(pa.BufferReader(raw))
        for batch in table.to_batches():
            for i in range(batch.num_rows):
                row = {name: batch.column(name)[i].as_py() for name in batch.schema.names}
                pair = project_to_pair(row)
                if not pair["prompt"] and not pair["response"]:
                    continue
                rows.append(pair)
    except Exception:
        # Fallback: try JSON lines or JSON array
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                items = decoded
            else:
                items = [decoded]
            for item in items:
                pair = project_to_pair(item)
                if not pair["prompt"] and not pair["response"]:
                    continue
                rows.append(pair)
        except Exception:
            # Last fallback: line-by-line jsonl
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    pair = project_to_pair(item)
                    if not pair["prompt"] and not pair["response"]:
                        continue
                    rows.append(pair)
                except Exception:
                    continue

    # Dedup by content hash (central store)
    kept: List[Dict[str, str]] = []
    for pair in rows:
        content = (pair["prompt"] + "\n" + pair["response"]).strip()
        if not content:
            continue
        h = sha256_bytes(content.encode("utf-8"))
        if dedup.seen(h):
            continue
        dedup.add(h)
        kept.append(pair)
    return kept

def build_shard(
    repo: str,
    files: List[Dict[str, Any]],
    shard_id: int,
    shard
