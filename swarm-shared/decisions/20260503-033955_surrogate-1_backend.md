# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single `list_repo_tree` call (outside rate-limited ingestion) produces `file-list.json` committed to repo or passed via artifact; worker loads this manifest and only touches its deterministic shard
- **CDN-only downloads**: uses `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}` with no Authorization header → bypasses `/api/` 429 limits
- Projects heterogeneous HF datasets to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`)
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL`
- Central md5 dedup via existing `lib/dedup.py` (SQLite)
- Outputs: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Retry/backoff for 429 (wait 360s) and transient errors
- Runs in GitHub Actions matrix (16 shards) and can be invoked locally for testing

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage (GH Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py

Local test:
  DATE_FOLDER=2026-05-03 SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
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
from huggingface_hub import HfApi

# Project imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

# ---- config ----
REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

RAW_BASE_PATH = "raw"  # e.g. raw/2026-05-03/*.parquet
MANIFEST_PATH = Path("file-list.json")

OUT_DIR = Path("batches/public-merged") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

RATE_LIMIT_RETRY = 360
MAX_RETRIES = 5

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# ---- hf client ----
api = HfApi(token=HF_TOKEN)

def list_date_files(date_folder: str) -> List[str]:
    """Single API call to list files in date folder."""
    folder_path = f"{RAW_BASE_PATH}/{date_folder}"
    log.info("Listing repo tree: %s/%s", REPO_ID, folder_path)
    try:
        items = api.list_repo_tree(repo_id=REPO_ID, path=folder_path, recursive=False)
    except Exception as exc:
        log.warning("list_repo_tree failed, falling back to repo root scan: %s", exc)
        items = api.list_repo_tree(repo_id=REPO_ID, path="", recursive=False)
        items = [it for it in items if it.rfilename.startswith(folder_path)]
    files = [it.rfilename for it in items if not it.rfilename.endswith("/")]
    log.info("Found %d files in %s", len(files), folder_path)
    return files

def save_manifest(files: List[str], date_folder: str) -> Path:
    manifest_path = MANIFEST_PATH
    manifest_path.write_text(json.dumps({"date": date_folder, "files": files}, indent=2))
    log.info("Manifest saved to %s", manifest_path)
    return manifest_path

def load_manifest() -> Dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text())

def should_process_file(filepath: str, shard_id: int, shard_total: int) -> bool:
    """Deterministic shard assignment by slug (filename without extension)."""
    slug = Path(filepath).stem or filepath
    digest = int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16)
    return (digest % shard_total) == shard_id

def resolve_cdn_url(filepath: str) -> str:
    """CDN bypass URL (no auth)."""
    return f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{filepath}"

def robust_get(url: str, **kwargs) -> requests.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=60, **kwargs)
            if resp.status_code == 429:
                wait = RATE_LIMIT_RETRY
                log.warning("Rate limited 429 (attempt %d). Waiting %ds.", attempt, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            wait = min(2 ** attempt, 60)
            log.warning("Request error (attempt %d): %s. Retrying in %ds.", attempt, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts")

def parse_file_to_pairs(filepath: str) -> List[Dict[str, str]]:
    """
    Download file via CDN and project to {prompt, response}.
    Supports common formats: jsonl, parquet (via pyarrow), json.
    Avoids load_dataset(streaming=True) on heterogeneous schemas.
    """
    url = resolve_cdn_url(filepath)
    suffix = Path(filepath).suffix.lower()
    resp = robust_get(url)
    data = resp.content

    if suffix == ".parquet":
        import pyarrow.parquet as pq
        import io
        table = pq.read_table(io.BytesIO(data))
        rows = table.to_pylist()
    elif suffix == ".jsonl":
        rows = [json.loads(ln) for ln in data.decode("utf-8").strip().splitlines() if ln.strip()]
    elif suffix == ".json":
        rows = json.loads(data.decode("utf-8"))
        if isinstance(rows, dict) and "data" in rows:
            rows = rows["data"]
        if not isinstance(rows, list):
            rows = [rows]
    else:
        log.warning("Unsupported file type %s for %s; skipping", suffix, filepath)
        return []

    pairs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Flexible field mapping
        prompt = row.get("prompt") or row.get("instruction") or row.get("input") or row.get("question")
        response = row.get("response") or row.get("completion") or row.get("output") or row.get("answer")
        if prompt is None or response is None:
            messages = row.get("messages")
            if isinstance(messages, list) and len(messages) >= 2:
                prompt = messages[0].get("content")
                response = messages[-1].get("content")
        if prompt is not None and response is not None:
            pairs.append({"prompt": str(prompt), "response": str(response)})
    return pairs

def main() -> None:
    log.info("surrogate-ingest worker shard=%d/%d date=%s", SHARD_ID, SHARD_TOTAL, DATE_FOLDER)

    # 1) manifest: load existing or create once (single
