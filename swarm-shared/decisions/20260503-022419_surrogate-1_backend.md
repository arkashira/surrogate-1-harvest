# surrogate-1 / backend

## Final Unified Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses **manifest file list** (generated once on Mac) to avoid HF API rate limits during training
- Downloads via **HF CDN** (`resolve/main/`) — no Authorization header, bypasses `/api/` 429 limits
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Dedups via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on fatal error

### Steps (1h 45m)

1. **Create `bin/dataset-enrich.py`** (60m) — manifest loader, CDN downloader, schema projector, dedup, uploader  
2. **Create `bin/gen-manifest.py`** (15m) — one-off Mac script to list date folder via HF API once, save `manifest-{DATE}.json`  
3. **Update `.github/workflows/ingest.yml`** (15m) — add `DATE` env, pass manifest artifact, use Python worker  
4. **Add `requirements-dev.txt`** (5m) — `requests`, `tqdm`, `huggingface_hub`, `pyarrow`  
5. **Smoke test** (10m) — run locally with a small manifest slice

---

## Final Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py --repo axentx/surrogate-1-training-pairs \
                               --manifest manifest-2026-04-29.json

Behavior:
- Reads file list from manifest (generated once on Mac via gen-manifest.py)
- Each shard processes its deterministic slice (hash-based modulo)
- Downloads via HF CDN (no auth header) to bypass /api/ rate limits
- Projects heterogeneous files to {prompt, response}
- Dedups via lib/dedup.py central md5 store
- Writes batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
- Uploads to HF dataset repo via huggingface_hub
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
BATCH_DIR_TEMPLATE = "batches/public-merged/{date}"

# Known schema projections: keep only prompt/response fields
PROMPT_KEYS = {"prompt", "instruction", "question", "input", "messages"}
RESPONSE_KEYS = {"response", "answer", "output", "completion", "choices"}

def _hash_slug(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

def _is_parquet(path: str) -> bool:
    return path.lower().endswith((".parquet", ".pq"))

def _is_jsonl(path: str) -> bool:
    return path.lower().endswith(".jsonl")

def _download_cdn(repo: str, path: str, dest: Path) -> Path:
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    resp = requests.get(url, timeout=30, stream=True)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest

def _extract_pair_from_parquet(path: Path) -> List[Dict[str, str]]:
    """Project heterogeneous parquet to {prompt, response} only."""
    try:
        table = pq.read_table(path, columns=None)
    except pa.lib.ArrowInvalid:
        # Fallback: read all and project
        table = pq.read_table(path)

    cols = {c.lower(): c for c in table.column_names}
    prompt_col = None
    response_col = None

    for pk in PROMPT_KEYS:
        if pk in cols:
            prompt_col = cols[pk]
            break
    for rk in RESPONSE_KEYS:
        if rk in cols:
            response_col = cols[rk]
            break

    # If messages-like column exists, try to flatten last assistant message
    if prompt_col is None and "messages" in cols:
        messages_col = cols["messages"]
        # Convert to list of dicts and extract
        messages = table.column(messages_col).to_pylist()
        pairs = []
        for msg_list in messages:
            if not isinstance(msg_list, list):
                continue
            prompt_parts = []
            response_part = None
            for m in msg_list:
                if isinstance(m, dict):
                    role = m.get("role", "")
                    content = m.get("content", "")
                    if role == "assistant":
                        response_part = content
                    else:
                        prompt_parts.append(content)
            if response_part is not None:
                pairs.append({"prompt": "\n".join(prompt_parts).strip(), "response": response_part})
        return pairs

    if prompt_col is None or response_col is None:
        # Best-effort: pick two fields
        non_keys = [c for c in table.column_names if c.lower() not in PROMPT_KEYS | RESPONSE_KEYS]
        if len(table.column_names) >= 2:
            prompt_col = table.column_names[0]
            response_col = table.column_names[1]
        elif len(table.column_names) == 1:
            prompt_col = table.column_names[0]
            response_col = ""
        else:
            return []

    prompt_data = table.column(prompt_col).to_pylist() if prompt_col else [""] * len(table)
    response_data = table.column(response_col).to_pylist() if response_col else [""] * len(table)

    pairs = []
    for p, r in zip(prompt_data, response_data):
        pairs.append({"prompt": str(p).strip(), "response": str(r).strip()})
    return pairs

def _extract_pair_from_jsonl(path: Path) -> List[Dict[str, str]]:
    pairs = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(obj, dict):
                continue

            prompt = None
            response = None

            for pk in PROMPT_KEYS:
                if pk in obj:
                    prompt = obj[pk]
                    break
            for rk in RESPONSE_KEYS:
                if rk in obj:
                    response = obj[rk]
                    break

            # Fallback: try messages
            if prompt is None and "messages" in obj and isinstance(obj["messages"], list):
                msg_list = obj["messages"]
                prompt_parts = []
                response_part = None
                for m in msg_list:
                    if isinstance(m, dict):
                        role = m.get("role", "")
                        content = m.get("content", "")
                        if role == "assistant":
                            response_part = content
                        else:
                            prompt_parts.append(content)
                if response_part is not None:
                    prompt = "\n".join(prompt_parts).strip()
                    response = response_part

            if prompt is None or response is None:
                # Best-effort: pick two fields
                keys = list(obj.keys())

