# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **pre-listed manifest** (`manifest-{DATE_FOLDER}.json`) produced by the orchestrator/Mac to avoid recursive HF API calls and rate limits.
- Downloads assigned file paths via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429 limits).
- Projects each file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` mixed-schema CastError).
- Deduplicates via central `lib/dedup.py` md5 store and writes to `batches/public-merged/{DATE_FOLDER}/shard{N}-{HHMMSS}.jsonl`.
- Is idempotent and safe to rerun (same shard+date+timestamp → same output).

### Steps (1h 30m)

1. (10m) Inspect current `bin/dataset-enrich.sh` and `lib/dedup.py` to confirm interfaces.
2. (20m) Write `bin/dataset-enrich.py` implementing manifest load, CDN fetch, schema projection, dedup, and JSONL output.
3. (10m) Add lightweight retry/backoff for CDN downloads and handle HF 429 fallback wait.
4. (10m) Ensure executable bit and shebang; keep Bash wrapper for cron compatibility.
5. (20m) Update `.github/workflows/ingest.yml` to generate/pass manifest and `DATE_FOLDER` to matrix jobs.
6. (20m) Test locally with a small manifest slice; verify output schema and dedup behavior.

---

## Code Snippets

### bin/dataset-enrich.py
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 \
  DATE_FOLDER=2026-05-03 \
  python bin/dataset-enrich.py

Expects:
  - manifest-{DATE_FOLDER}.json in repo root (or path via MANIFEST_PATH)
    containing list of file paths in the HF dataset repo.
  - HF_TOKEN in environment for upload (read via CDN does not require token).
"""

import json
import os
import sys
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import requests
import pyarrow as pa
import pyarrow.parquet as pq

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", f"manifest-{DATE_FOLDER}.json")

OUT_DIR = Path("batches/public-merged") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Timestamp for this shard run (same across retries within the same minute keeps deterministic)
RUN_TS = datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{RUN_TS}.jsonl"

# Retry config
MAX_RETRIES = 5
BACKOFF_FACTOR = 2
HTTP_TIMEOUT = 30

dedup = DedupStore()

def shard_filter(path: str) -> bool:
    """Deterministic shard assignment by path hash."""
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return (h % SHARD_TOTAL) == SHARD_ID

def load_manifest() -> List[str]:
    if not Path(MANIFEST_PATH).exists():
        log.error("Manifest not found: %s", MANIFEST_PATH)
        sys.exit(1)
    with open(MANIFEST_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        files = data["files"]
    elif isinstance(data, list):
        files = data
    else:
        log.error("Manifest must be list of paths or dict with 'files' key")
        sys.exit(1)
    return [p for p in files if isinstance(p, str) and p.strip()]

def cdn_download(path: str) -> bytes:
    url = f"{CDN_BASE}/{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT, stream=False)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60 * 6))  # 6 min fallback
                log.warning("CDN 429, waiting %ss for %s", wait, path)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                log.error("Failed to download %s after %s attempts: %s", path, MAX_RETRIES, exc)
                raise
            sleep = BACKOFF_FACTOR ** attempt
            log.warning("Retry %s/%s for %s in %ss: %s", attempt, MAX_RETRIES, path, sleep, exc)
            time.sleep(sleep)
    raise RuntimeError(f"Unreachable: failed to download {path}")

def extract_pairs_from_parquet(content: bytes) -> List[Dict[str, str]]:
    try:
        table = pq.read_table(pa.BufferReader(content))
    except Exception as exc:
        log.warning("Parquet read failed: %s", exc)
        return []

    pairs = []
    # Try common column names; project only prompt/response
    prompt_col = None
    response_col = None
    for col in table.column_names:
        lc = col.lower()
        if "prompt" in lc:
            prompt_col = col
        if "response" in lc or "completion" in lc or "answer" in lc:
            response_col = col

    if prompt_col is None or response_col is None:
        # Fallback: if exactly two columns, treat as prompt/response
        if len(table.column_names) == 2:
            prompt_col, response_col = table.column_names
        else:
            log.warning("Cannot project prompt/response from columns: %s", table.column_names)
            return []

    prompts = table.column(prompt_col).to_pylist()
    responses = table.column(response_col).to_pylist()
    for p, r in zip(prompts, responses):
        if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
            pairs.append({"prompt": p.strip(), "response": r.strip()})
    return pairs

def extract_pairs_from_jsonl(content: bytes) -> List[Dict[str, str]]:
    out = []
    for line in content.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        prompt = None
        response = None

        if isinstance(obj, dict):
            # Prefer explicit prompt/response
            prompt = obj.get("prompt")
            response = obj.get("response")
            # Fallback to messages format: last assistant message as response, prior user as prompt
            if (prompt is None or response is None) and isinstance(obj.get("messages"), list) and len(obj["messages"]) >= 2:
                # Use last user-assistant pair when available
                messages = obj["messages"]
                # Find last user then next assistant
                for i in range(len(messages) - 1, -1
