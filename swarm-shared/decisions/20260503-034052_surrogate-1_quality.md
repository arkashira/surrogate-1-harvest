# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single `list_repo_tree` call → saves `manifest.json`; workers deterministically shard by `hash(rel_path) % SHARD_TOTAL`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → avoids 429 API limits during data load
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store (unchanged)
- Outputs `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with newline JSON (one pair per line)
- Adds retry/backoff for CDN 429/503 and commit-cap spreading via deterministic repo selection if needed
- Keeps script executable, Bash shebang wrapper for cron compatibility

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID          (required) 0..15
  SHARD_TOTAL       (default 16)
  DATE_FOLDER       (default today YYYY-MM-DD)
  HF_TOKEN          write token for axentx/surrogate-1-training-pairs
  MANIFEST_REPO     (default axentx/surrogate-1-training-pairs)
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
from huggingface_hub import HfApi, list_repo_tree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dataset-enrich")

# ---- config ----
SHARD_ID = int(os.getenv("SHARD_ID", -1))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
MANIFEST_REPO = os.getenv("MANIFEST_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
BASE_DATASET_PATH = ""  # root of dataset repo

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    log.error("Invalid SHARD_ID=%s (SHARD_TOTAL=%s)", SHARD_ID, SHARD_TOTAL)
    sys.exit(1)

# ---- utils ----
def deterministic_shard(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def cdn_download(url: str, timeout: int = 30) -> bytes:
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 429:
                wait = 360
                log.warning("CDN 429, waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Download failed (attempt %s): %s — retry in %ss", attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download after retries: {url}")

def list_files_once() -> List[str]:
    """Single API call to list top-level date folder; returns relative paths."""
    api = HfApi(token=HF_TOKEN)
    tree = list_repo_tree(
        repo_id=MANIFEST_REPO,
        path=BASE_DATASET_PATH.rstrip("/"),
        repo_type="dataset",
        recursive=False,
    )
    # Expect folders like YYYY-MM-DD/ containing parquet/jsonl files
    files = []
    for item in tree:
        if item.type == "file":
            files.append(item.path)
        elif item.type == "folder" and item.path == DATE_FOLDER:
            sub = list_repo_tree(
                repo_id=MANIFEST_REPO,
                path=item.path,
                repo_type="dataset",
                recursive=True,
            )
            files.extend(p.path for p in sub if p.type == "file")
    # If DATE_FOLDER not found, fallback to all files
    if not any(f.startswith(DATE_FOLDER) for f in files):
        files = [t.path for t in tree if t.type == "file"]
    return files

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schema to {prompt, response}.
    Heuristic field names commonly used across ingested sources.
    """
    prompt_keys = {"prompt", "instruction", "input", "question", "text", "content"}
    response_keys = {"response", "completion", "output", "answer", "result"}

    prompt = None
    response = None

    for k, v in raw.items():
        if not isinstance(v, (str, int, float)):
            continue
        sk = k.lower()
        if sk in prompt_keys and prompt is None:
            prompt = str(v)
        if sk in response_keys and response is None:
            response = str(v)

    # Fallbacks
    if prompt is None and response is None:
        # try first/second string fields
        str_vals = [str(v) for v in raw.values() if isinstance(v, (str, int, float))]
        if len(str_vals) >= 2:
            prompt, response = str_vals[0], str_vals[1]
        elif len(str_vals) == 1:
            prompt, response = str_vals[0], ""
        else:
            prompt, response = "", ""
    elif prompt is None:
        prompt = ""
    elif response is None:
        response = ""

    return {"prompt": prompt.strip(), "response": response.strip()}

# ---- main ----
def main() -> None:
    log.info("Starting worker shard=%s/%s date=%s", SHARD_ID, SHARD_TOTAL, DATE_FOLDER)

    # 1) manifest
    files = list_files_once()
    log.info("Found %s files", len(files))

    # Filter to supported file types
    supported_ext = {".jsonl", ".json", ".parquet", ".csv"}
    files = [f for f in files if Path(f).suffix.lower() in supported_ext]
    log.info("Supported files: %s", len(files))

    # 2) shard assignment
    my_files = [f for f in files if deterministic_shard(f, SHARD_TOTAL) == SHARD_ID]
    log.info("Shard %s assigned %s files", SHARD_ID, len(my_files))

    # 3) process
    out_dir = Path("batches/public-merged") / DATE_FOLDER
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    from lib.dedup import DedupStore  # local dedup wrapper

    dedup = DedupStore()
    written = 0

    for rel_path in sorted(my_files):
        try:
            # CDN bypass URL (no auth header)
            cdn_url = f"https://huggingface.co/datasets/{MANIFEST_REPO}/resolve/main/{rel_path}"
            data = cdn_download(cdn_url)

            ext = Path(rel_path).suffix.lower()
            if ext == ".parquet":
                import pyarrow.parquet as pq
                import io
                table = pq.read_table(io.BytesIO(data))
                rows = table.to_pylist()
            elif ext == ".jsonl":
                rows = [json.loads(l) for l in data.decode().strip().splitlines() if l.strip()]
            elif ext == ".json":
                rows = json.loads(data.decode())
                if isinstance(rows, dict):
                    rows
