# surrogate-1 / quality

## Final Implementation Plan (≤2 h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during data loads and prevents mixed-schema `CastError`s. One worker run is deterministic, observable, and safe for 16-way parallelism.

---

### Changes

1. **Add `bin/worker.py`** — single-file worker:
   - Accepts `SHARD_ID` (0–15) and `TOTAL_SHARDS` (default 16) via env.
   - One API call (`list_repo_tree`) to list date folders under `batches/public-merged/`.
   - Picks the latest date folder.
   - Builds a manifest of all `shard-*.jsonl` files in that folder.
   - Filters files by `int(sha256(filename) % TOTAL_SHARDS) == SHARD_ID`.
   - Downloads selected files via **HF CDN** (`resolve/main/...`) with no Authorization header.
   - Streams JSONL, projects to `{prompt, response}`, validates schema, skips malformed rows.
   - Deduplicates via `lib/dedup.py` (central md5 store).
   - Writes output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` via HF API (single commit).

2. **Replace `bin/dataset-enrich.sh` with `bin/dataset_enrich.py`** (thin wrapper):
   - Exports `SHARD_ID`, `TOTAL_SHARDS=16`.
   - Invokes `python3 bin/worker.py`.
   - Logs to stdout/stderr (captured by Actions).

3. **Update `.github/workflows/ingest.yml`**:
   - Add matrix strategy with `shard_id: [0..15]`.
   - Keep 7 GB runner headroom and short timeout per shard.

4. **Add `requirements-dev.txt`** (optional) — pin `requests`, `tqdm`, `huggingface-hub` for local testing.

---

### Why this is highest value
- Eliminates HF API rate limits during data load (CDN bypass).
- Prevents `pyarrow.CastError` by projecting schema at parse time.
- Keeps 16-shard parallelism while making each shard resilient and observable.
- Fits within 2 h: ~150 LoC new + small workflow tweak.

---

### Code Snippets

#### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Environment:
  SHARD_ID        int 0..15
  TOTAL_SHARDS    int (default 16)
  HF_TOKEN        write token for axentx/surrogate-1-training-pairs
  DATASET_REPO    default axentx/surrogate-1-training-pairs
"""

import os
import sys
import json
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Iterable

import requests
from huggingface_hub import HfApi, list_repo_tree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("worker")

HF_TOKEN = os.getenv("HF_TOKEN")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "-1"))

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)
if not (0 <= SHARD_ID < TOTAL_SHARDS):
    log.error("SHARD_ID must be in [0, TOTAL_SHARDS-1]")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# Local dedup store
DEDUP_DB_PATH = Path(__file__).parent / "lib" / "dedup.py"
if DEDUP_DB_PATH.exists():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", DEDUP_DB_PATH)
    dedup_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dedup_mod)
    is_duplicate = getattr(dedup_mod, "is_duplicate", None)
    mark_seen = getattr(dedup_mod, "mark_seen", None)
else:
    log.warning("dedup.py not found, skipping cross-run dedup")
    is_duplicate = lambda h: False  # noqa: E731
    mark_seen = lambda h: None  # noqa: E731

CDN_ROOT = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main"

def deterministic_shard(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % TOTAL_SHARDS

def list_date_folders() -> list[str]:
    """Return sorted date folder names under batches/public-merged/"""
    items = list_repo_tree(
        repo_id=DATASET_REPO,
        path="batches/public-merged",
        repo_type="dataset",
    )
    folders = [it.rfilename for it in items if it.type == "directory"]
    folders.sort(reverse=True)
    return folders

def list_shard_files(date_folder: str) -> list[str]:
    """List all shard-*.jsonl files in given date folder."""
    path = f"batches/public-merged/{date_folder}"
    items = list_repo_tree(
        repo_id=DATASET_REPO,
        path=path,
        repo_type="dataset",
    )
    files = [it.rfilename for it in items if it.type == "file" and it.rfilename.endswith(".jsonl")]
    files.sort()
    return files

def cdn_download(url: str, chunk_size: int = 8192) -> Iterable[bytes]:
    """Stream from CDN without auth."""
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    for chunk in resp.iter_content(chunk_size=chunk_size):
        if chunk:
            yield chunk

def project_record(raw: Dict[str, Any]) -> Dict[str, str] | None:
    """
    Project heterogeneous schemas to {prompt, response}.
    Returns None if record is malformed.
    """
    if not isinstance(raw, dict):
        return None

    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")

    if prompt is None or response is None:
        return None

    prompt = str(prompt).strip()
    response = str(response).strip()
    if not prompt or not response:
        return None

    return {"prompt": prompt, "response": response}

def hash_record(record: Dict[str, str]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.md5(payload).hexdigest()

def run_worker():
    log.info("Starting worker shard=%d/%d", SHARD_ID, TOTAL_SHARDS)

    # 1) Pick latest date folder
    dates = list_date_folders()
    if not dates:
        log.error("No date folders found under batches/public-merged/")
        sys.exit(1)
    date_folder = dates[0]
    log.info("Using date folder: %s", date_folder)

    # 2) List and filter files for this shard
    files = list_shard_files(date_folder)
    my_files = [f for f in files if deterministic_shard(f) == SHARD_ID]
    log.info("Total files=%d, assigned=%d", len(files), len(my_files))

    # 3) Process files
    out_records = []
    seen_local = set()
    processed = 0
    skipped_dup = 0
    skipped_bad = 0

    base_path = f"batches/public-merged/{date_folder}"
    for rfilename in my_files:
        cdn_url = f"{CDN_ROOT}/{base_path}/{rfilename}"
        log.info("Downloading %s", rfilename)

        buffer = b""
        for chunk in cdn_download(cdn_url):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer
