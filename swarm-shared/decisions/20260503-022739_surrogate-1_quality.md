# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- **Manifest**: orchestrator (or first run) performs one `list_repo_tree` for `batches/raw/{DATE}/` → `file-list-{DATE}.json`; workers load this manifest and shard by `hash(rel_path) % SHARD_TOTAL` (deterministic, stable across reruns)
- **CDN bypass**: downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no Authorization header) to avoid API 429s
- **Schema projection**: maps heterogeneous inputs to `{prompt, response}` at parse time; never uses `load_dataset(streaming=True)` on mixed schemas
- **Deduplication**: central `lib/dedup.py` md5 store checked before emitting
- **Output**: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic naming; single writer per shard prevents collisions
- **Reliability**: retries with jittered exponential backoff; 429 → wait 360s; 403/503/5xx → backoff; connection errors → retry; per-file timeout and size cap to avoid hangs

### Code changes

#### 1) New: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (env):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx REPO_ID=axentx/surrogate-1-training-pairs \
  python bin/dataset-enrich.py

Behavior:
- Loads manifest file-list-{DATE}.json (or falls back to listing once and saving it).
- Deterministic sharding by hash(rel_path) % SHARD_TOTAL.
- Downloads via CDN (no auth header) to avoid API rate limits.
- Projects heterogeneous schemas to {prompt, response}.
- Deduplicates via lib/dedup.py (md5 store).
- Writes shard output to batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl
"""
import os
import sys
import json
import time
import hashlib
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# ---------- config ----------
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
OUTPUT_ROOT = Path("batches/public-merged")
MAX_FILE_SIZE = 512 * 1024 * 1024  # 512 MiB cap per file to avoid OOM hangs
REQUEST_TIMEOUT = 30

# ---------- dedup ----------
def _import_dedup():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", "lib/dedup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def is_duplicate(md5_hex: str) -> bool:
    try:
        mod = _import_dedup()
        return mod.is_duplicate(md5_hex)
    except Exception as exc:
        log.warning("Dedup unavailable: %s", exc)
        return False

def mark_seen(md5_hex: str) -> None:
    try:
        mod = _import_dedup()
        mod.mark_seen(md5_hex)
    except Exception as exc:
        log.warning("Could not mark seen: %s", exc)

# ---------- utils ----------
def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % total

def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%H%M%S")

# ---------- CDN download ----------
def cdn_get(path: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    """Download via CDN (no Authorization header)."""
    url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{path}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=timeout, headers={}, stream=True)
            if resp.status_code == 429:
                wait = 360
                log.warning("429 rate-limited, waiting %ss (url=%s)", wait, path)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            # stream to avoid large in-memory spikes; enforce size cap
            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > MAX_FILE_SIZE:
                    raise RuntimeError(f"File exceeds size cap: {path}")
            return content
        except requests.RequestException as exc:
            wait = min((2 ** attempt) + (hash(url) % 10) / 10.0, 60)
            log.warning("Download failed %s: %s — retry in %.1fs", path, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download after retries: {path}")

# ---------- manifest handling ----------
def list_files_for_date(date: str) -> List[str]:
    """
    List top-level parquet files under batches/raw/{date}/ using repo_tree.
    Intended to be run once per cron tick (or by orchestrator) and saved as manifest.
    """
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN required for listing")
    url = f"https://huggingface.co/api/datasets/{REPO_ID}/tree/batches/raw/{date}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 429:
                time.sleep(360)
                continue
            resp.raise_for_status()
            items = resp.json()
            paths = [p["path"] for p in items if isinstance(p, dict) and p.get("type") == "file" and p["path"].endswith(".parquet")]
            log.info("Listed %d parquet files for %s", len(paths), date)
            return paths
        except requests.RequestException as exc:
            wait = (2 ** attempt) + 5
            log.warning("List failed: %s — retry in %.1fs", exc, wait)
            time.sleep(wait)
    raise RuntimeError("Failed to list files")

def load_or_create_manifest(date: str, work_dir: Path) -> List[str]:
    manifest_path = work_dir / f"file-list-{date}.json"
    if manifest_path.is_file():
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and all(isinstance(x, str) for x in data):
                    log.info("Loaded existing manifest: %s", manifest_path)
                    return data
        except Exception as exc:
            log.warning("Could not load manifest %s: %s", manifest_path, exc)

    # create manifest
    paths = list_files_for_date(date)
    try:
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(paths, f)
        log.info("Saved manifest: %s", manifest_path)
    except Exception as exc:
        log.warning("Could not save manifest %s: %s", manifest_path, exc)
    return paths

# ---------- projection ----------
def project_to_pair(raw_obj: Dict[str
