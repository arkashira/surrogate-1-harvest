# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date on the Mac orchestrator and committed to the repo (or passed via env) so each GitHub runner avoids recursive `list_repo_files` API calls and HF rate limits.
- Downloads only its deterministic 1/16 shard via **raw CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header — bypasses `/api/` rate limits entirely.
- Projects heterogeneous file schemas to `{prompt, response}` at parse time (avoids pyarrow `CastError` from `load_dataset(streaming=True)`).
- Deduplicates via the existing central md5 store (`lib/dedup.py`).
- Writes output as `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with deterministic naming to prevent cross-shard collisions.
- Adds a small Mac-side helper (`bin/gen-manifest.py`) to produce the manifest JSON from `list_repo_tree` (non-recursive per date folder) and save it so runners can operate API-free.

### Steps (timed)

1. **Read current files** (5m) — inspect `bin/dataset-enrich.sh`, `lib/dedup.py`, `.github/workflows/ingest.yml`, `requirements.txt`.
2. **Create `bin/dataset-enrich.py`** (45m) — manifest loader, CDN downloader, per-schema projector, dedup integration, JSONL writer.
3. **Create `bin/gen-manifest.py`** (15m) — Mac orchestrator helper to snapshot one date folder via `list_repo_tree` and emit `manifest-<date>.json`.
4. **Update workflow** (15m) — pass `MANIFEST_PATH` (or embed manifest in repo), set `SHARD_ID`/`N_SHARDS`, ensure `HF_TOKEN` only used for push (not reads).
5. **Update requirements** (5m) — ensure `requests` present; keep `datasets`/`huggingface_hub` for upload only.
6. **Test locally** (20m) — run worker against a small date slice with mocked CDN URLs or real public files; verify projection and dedup.
7. **Commit & push** (5m).

Total: ~1h 55m.

---

## File: bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 N_SHARDS=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --manifest manifest-2026-05-03.json \
    --out-dir batches/public-merged

Behavior:
- Loads file manifest (list of paths under date folder).
- Keeps only paths assigned to this shard by deterministic hash.
- Downloads each file via HF CDN (no auth) and projects to {prompt,response}.
- Deduplicates via lib.dedup by md5 of normalized pair.
- Writes JSONL to out_dir/date/shard<N>-<HHMMSS>.jsonl
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

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# Local dedup store
DEDUP_DB_PATH = Path(__file__).parent.parent / "lib" / "dedup.py"
sys.path.insert(0, str(DEDUP_DB_PATH.parent))
from dedup import DedupStore  # type: ignore

HF_CDN_BASE = "https://huggingface.co/datasets"

# Common schema projections: map raw column names to {prompt,response}
COLUMN_MAPS = [
    # Generic
    {"prompt": ["prompt", "instruction", "input", "question"],
     "response": ["response", "output", "answer", "completion"]},
]

def shard_assign(key: str, n_shards: int) -> int:
    """Deterministic shard assignment by key."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:4], "little") % n_shards

def normalize_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Project heterogeneous schema to {prompt,response}."""
    if not isinstance(obj, dict):
        return None

    # If already correct shape, keep
    if "prompt" in obj and "response" in obj:
        prompt = str(obj["prompt"]).strip()
        response = str(obj["response"]).strip()
        if prompt and response:
            return {"prompt": prompt, "response": response}

    # Try column maps
    for cmap in COLUMN_MAPS:
        prompt_val = None
        response_val = None
        for pkey in cmap["prompt"]:
            if pkey in obj and obj[pkey] not in (None, ""):
                prompt_val = str(obj[pkey]).strip()
                break
        for rkey in cmap["response"]:
            if rkey in obj and obj[rkey] not in (None, ""):
                response_val = str(obj[rkey]).strip()
                break
        if prompt_val and response_val:
            return {"prompt": prompt_val, "response": response_val}

    # Fallback: look for any two text-ish fields
    text_keys = [k for k, v in obj.items() if isinstance(v, str) and len(v.strip()) > 10]
    if len(text_keys) >= 2:
        return {"prompt": obj[text_keys[0]].strip(), "response": obj[text_keys[1]].strip()}

    return None

def download_cdn(repo: str, path: str, timeout: int = 30) -> Optional[bytes]:
    """Download file via HF CDN (no auth)."""
    url = f"{HF_CDN_BASE}/{repo}/resolve/main/{path}"
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "axentx-surrogate-ingest"})
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"[WARN] CDN download failed for {path}: {exc}", file=sys.stderr)
        return None

def extract_jsonl_lines(content: bytes) -> Iterable[Dict[str, Any]]:
    """Yield JSON objects from JSONL content."""
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

def extract_parquet_rows(content: bytes) -> Iterable[Dict[str, Any]]:
    """Yield rows from parquet bytes, projecting to dicts."""
    try:
        table = pq.read_table(pa.BufferReader(content))
        for batch in table.to_batches(max_chunksize=10_000):
            cols = {name: batch.column(name).to_pylist() for name in batch.schema.names}
            n = len(next(iter(cols.values()))) if cols else 0
            for i in range(n):
                yield {k: v[i] for k, v in cols.items()}
    except Exception as exc:
        print(f"[WARN] Parquet decode failed: {exc}", file=sys.stderr)

def process_file(
    repo: str,
    path: str,
    dedup: DedupStore,
    seen_local: set,
) -> List[Dict[str, str]]:
    """Download, decode, project, and dedup a single file. Returns accepted pairs."""
    content = download_cdn(repo, path)
    if content is None:
        return []

    accepted: List[Dict[str, str]] = []
    ext = Path(path).suffix.lower()

    if ext == ".jsonl":
        rows = extract_jsonl_lines(content)
    elif ext == ".parquet":
        rows = extract_parquet_rows(content)
    else:
        print(f"[WARN] Skipping unsupported file {path}", file=sys.stderr)
        return []

    for raw in rows:
        pair = normalize_pair(raw)
        if not pair:
            continue

        # Deterministic md5 for dedup across sources
