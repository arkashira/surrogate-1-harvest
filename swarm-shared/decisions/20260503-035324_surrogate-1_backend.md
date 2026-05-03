# surrogate-1 / backend

Below is the **single, merged implementation** that keeps every high-value idea from both candidates, removes duplication, and resolves conflicts in favor of **correctness + concrete actionability**.

---

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed manifest** (`manifests/{DATE_FOLDER}.json`) produced by the Mac orchestrator (or a one-off GitHub Action) via **one `list_repo_tree` call after the HF rate-limit window**.  
  - If the manifest is absent, **fail fast with clear instructions** (do not attempt recursive listing during ingestion).
- Downloads each assigned file via **HF CDN direct URLs** (`https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}`) with **no Authorization header** — bypasses `/api/` auth checks entirely.
- Projects each file to `{prompt, response}` **only at parse time** (avoids `pyarrow.CastError` from mixed schemas).
- Deduplicates via the existing central md5 store (`lib/dedup.py`) **before emitting**.
- Writes output to `batches/public-merged/{DATE_FOLDER}/shard{SHARD_ID}-{HHMMSS}.jsonl` and **commits via HF API with automatic sibling-repo spillover** to avoid per-repo commit limits.
- Reuses a running Lightning Studio for any downstream training step (if triggered), otherwise exits cleanly.

Total estimated time: **90 minutes** (60m implementation + 30m smoke test).

---

## Code Changes

### 1) New Python worker (`bin/dataset-enrich.py`)

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID            - 0..15
  SHARD_TOTAL         - default 16
  DATE_FOLDER         - default today YYYY-MM-DD
  HF_TOKEN            - write token for axentx/surrogate-1-training-pairs
  MANIFEST_PATH       - optional path to pre-listed manifest JSON
"""
import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
import pyarrow as pa
import pyarrow.parquet as pq

# Add repo root to path for lib imports
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.dedup import DedupStore  # noqa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dataset-enrich")

HF_DATASET_REPO = "axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
HF_API_BASE = "https://huggingface.co/api"

# Rate-limit handling constants
HF_RATE_LIMIT_RETRY = 360  # seconds (per HF pattern)
MAX_RETRIES = 5

# Sibling repos for commit-cap spreading (if needed)
SIBLING_REPOS = [
    f"axentx/surrogate-1-training-pairs-{i}" for i in range(1, 6)
]


def shard_files(file_paths: List[str], shard_id: int, shard_total: int) -> List[str]:
    """Deterministic shard assignment by file path hash."""
    assigned = []
    for p in file_paths:
        bucket = int(hashlib.md5(p.encode()).hexdigest(), 16) % shard_total
        if bucket == shard_id:
            assigned.append(p)
    return assigned


def load_manifest(manifest_path: Optional[str], date_folder: str) -> List[str]:
    """
    Load pre-listed manifest for a date folder.

    If manifest_path is provided and exists, use it.
    Otherwise, try manifests/{date_folder}.json.
    If neither exists, fail fast (do NOT list during ingestion).
    """
    candidates = []
    if manifest_path:
        candidates.append(Path(manifest_path))
    candidates.append(REPO_ROOT / "manifests" / f"{date_folder}.json")

    for cand in candidates:
        if cand.exists():
            with open(cand) as f:
                data = json.load(f)
            paths = data.get(date_folder, [])
            if not isinstance(paths, list):
                log.error("Manifest %s does not contain a list for %s", cand, date_folder)
                return []
            log.info("Loaded %d paths from %s", len(paths), cand)
            return paths

    log.error(
        "No manifest found for %s. Create it with Mac orchestrator or one-off GH Action.",
        date_folder,
    )
    sys.exit(1)


def cdn_download(url: str, timeout: int = 30) -> bytes:
    """Download via HF CDN (no auth)."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=timeout, headers={})
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            wait = (2 ** attempt) * 5
            log.warning("Download failed %s (attempt %s): %s", url, attempt, exc)
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url}")


def parse_to_pair(raw: bytes, path: str) -> Dict[str, str]:
    """Project arbitrary file to {prompt, response} only."""
    suffix = Path(path).suffix.lower()

    if suffix == ".jsonl":
        lines = raw.decode("utf-8").strip().splitlines()
        objs = [json.loads(l) for l in lines if l.strip()]
        obj = objs[0] if objs else {}
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        return {"prompt": str(prompt), "response": str(response)}

    if suffix == ".parquet":
        try:
            table = pq.read_table(pa.BufferReader(raw), columns=["prompt", "response"])
            df = table.to_pydict()
            return {
                "prompt": str(df.get("prompt", [""])[0]),
                "response": str(df.get("response", [""])[0]),
            }
        except (pa.ArrowInvalid, KeyError, OSError):
            table = pq.read_table(pa.BufferReader(raw))
            cols = table.column_names
            prompt_col = next((c for c in ["prompt", "input", "question"] if c in cols), None)
            response_col = next((c for c in ["response", "output", "answer"] if c in cols), None)
            prompt_vals = table.column(prompt_col).to_pylist() if prompt_col else [""] * len(table)
            response_vals = table.column(response_col).to_pylist() if response_col else [""] * len(table)
            return {
                "prompt": str(prompt_vals[0]),
                "response": str(response_vals[0]),
            }

    # Fallback: treat as text
    text = raw.decode("utf-8", errors="replace")
    return {"prompt": "", "response": text}


def upload_to_hf(filepath: str, content: bytes, token: str, repo_type: str = "dataset") -> bool:
    """Upload single file via HF API (LFS)."""
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.put(
                f"{HF_API_BASE}/datasets/{HF_DATASET_REPO}/upload/{filepath}",
                headers=headers,
                data=content,
                timeout=120,
            )
            if resp.status_code == 429:
                log.warning("HF API 429, waiting %s", HF_RATE_LIMIT_RETRY)
                time
