# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses a single `list_repo_tree` call (per date folder) → saves `file-list.json`
- Downloads only assigned shard files via **HF CDN** (`resolve/main/...`) — zero API/auth calls during training
- Projects heterogeneous schemas to `{prompt,response}` at parse time (avoids PyArrow CastError)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits non-zero on unrecoverable errors; logs summary metrics

### Why this is the highest-value incremental improvement
- Fixes the **HF API rate-limit + schema CastError** patterns that block training
- Enables **Lightning Studio reuse + CDN-only training** (zero API calls during data load)
- Keeps GitHub Actions 16-shard parallelism while making each shard robust and observable
- <2h to ship: single-file replacement + minor workflow tweak

---

## 2. Code changes

### 2.1 `bin/dataset-enrich.py` (new)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py

Behavior:
- Lists files in axentx/surrogate-1-training-pairs for DATE folder once.
- Each shard processes its deterministic slice via slug-hash mod.
- Downloads via HF CDN (no auth/API during download).
- Projects to {prompt,response}; dedups via lib/dedup.py.
- Outputs batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
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

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# Project imports
try:
    from lib.dedup import is_duplicate, record_hash
except ImportError as e:
    # Fallback for direct execution without package layout
    logging.warning("lib.dedup not importable; using in-memory dedup only: %s", e)

    class _DedupFallback:
        def __init__(self) -> None:
            self.seen: set[str] = set()

        def is_duplicate(self, md5: str) -> bool:
            return md5 in self.seen

        def record_hash(self, md5: str) -> None:
            self.seen.add(md5)

    dedup = _DedupFallback()
    is_duplicate = dedup.is_duplicate  # type: ignore
    record_hash = dedup.record_hash  # type: ignore

# ---- constants ----
REPO_ID = "axentx/surrogate-1-training-pairs"
API = HfApi()
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")

# ---- utils ----
def slug_hash(slug: str) -> int:
    """Deterministic 64-bit hash for shard assignment."""
    return int(hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16], 16)

def list_date_files(date_folder: str) -> List[str]:
    """
    Single API call: list top-level files in date folder (non-recursive).
    Returns relative paths.
    """
    try:
        tree = list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
        files = [item.rfilename for item in tree if item.type == "file"]
        log.info("listed %d files in %s", len(files), date_folder)
        return files
    except Exception as exc:
        log.exception("failed to list_repo_tree for %s", date_folder)
        raise RuntimeError(f"list_repo_tree failed: {exc}") from exc

def download_via_cdn(rel_path: str, dst: Path) -> Path:
    """Download via HF CDN (no Authorization header)."""
    url = f"{CDN_ROOT}/{rel_path}"
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dst

def project_to_pair(raw: Dict[str, Any], rel_path: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file schemas to {prompt,response}.
    Returns None if unprojectable.
    """
    # Common patterns seen in surrogate-1 datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    # Normalize keys to lowercase for tolerant matching
    low = {k.lower(): (k, v) for k, v in raw.items() if isinstance(k, str)}

    prompt_val = None
    response_val = None

    for pk in prompt_keys:
        if pk in low:
            prompt_val = str(low[pk][1]).strip()
            break
    for rk in response_keys:
        if rk in low:
            response_val = str(low[rk][1]).strip()
            break

    # Fallback: if exactly two text fields, treat as prompt/response
    if prompt_val is None or response_val is None:
        text_fields = [str(v).strip() for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(text_fields) == 2:
            prompt_val, response_val = text_fields[0], text_fields[1]

    if not prompt_val or not response_val:
        log.debug("unprojectable %s: keys=%s", rel_path, list(raw.keys()))
        return None

    return {"prompt": prompt_val, "response": response_val}

def parse_file(path: Path, rel_path: str) -> Iterable[Dict[str, str]]:
    """
    Parse common formats (jsonl, json, parquet) and yield projected pairs.
    """
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("invalid jsonl line %s:%d", rel_path, line_no)
                    continue
                pair = project_to_pair(raw, rel_path)
                if pair:
                    yield pair
    elif suffix == ".json":
        with open(path, "r", encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except json.JSONDecodeError:
                log.warning("invalid json %s", rel_path)
                return
        # Support both list-of-records and single record
        items = raw if isinstance(raw, list) else [raw]
        for raw_item in items:
            pair = project_to_pair(raw_item, rel_path)
            if pair:
                yield pair
    elif suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(path)
            df = table.to_pandas()
        except Exception as exc:
            log.warning("failed to read parquet %s: %s", rel_path, exc)
            return
        for _, row in df.iterrows():
            raw = row.to_dict()
            pair = project_to_pair(raw, rel_path)
            if pair:
                yield pair
    else:

