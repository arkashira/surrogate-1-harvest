# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner (or a pre-generated manifest) to list files in `{DATE_FOLDER}/` via `list_repo_tree(recursive=False)`, then deterministically assigns each file to a shard by `hash(slug) % SHARD_TOTAL`.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to avoid `/api/` rate limits.
- Projects each file to `{prompt, response}` at parse time (no schema assumptions), computes `md5` for dedup, and streams output to `batches/public-merged/{DATE_FOLDER}/shard{SHARD_ID}-{HHMMSS}.jsonl`.
- Uses the existing `lib/dedup.py` central md5 store for cross-run dedup (best-effort; duplicates across runs are acceptable per trade-offs).
- Exits cleanly on 429 with 360s backoff; logs per-file progress and shard assignment for observability.

---

## Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass shard worker.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID          - required, 0..SHARD_TOTAL-1
  SHARD_TOTAL       - optional, default 16
  DATE_FOLDER       - optional, default today YYYY-MM-DD
  HF_TOKEN          - optional, for write to dataset repo
  REPO_ID           - optional, default axentx/surrogate-1-training-pairs
  OUTPUT_DIR        - optional, default ./output
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ---- config ----
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", -1))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))
API_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
# retry/backoff
MAX_RETRIES = 5
BACKOFF_ON_429 = 360

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    print(f"Invalid SHARD_ID={SHARD_ID} (must be 0..{SHARD_TOTAL-1})", file=sys.stderr)
    sys.exit(1)

# ---- dedup ----
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

dedup = DedupStore()

# ---- utils ----
def backoff_sleep(attempt: int):
    t = (2**attempt) + (hashlib.sha256(os.urandom(8)).hexdigest(),)  # jitter
    # simple deterministic-ish jitter
    jitter = int(hashlib.md5(f"{attempt}{time.time()}".encode()).hexdigest()[:4], 16) / 1000.0
    time.sleep(min((2**attempt) + jitter, 60))

def robust_get(url: str, stream=False):
    headers = {}
    # CDN bypass: no Authorization header
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, stream=stream, timeout=60)
            if resp.status_code == 429:
                print(f"Rate limited 429 on {url}, sleeping {BACKOFF_ON_429}s", file=sys.stderr)
                time.sleep(BACKOFF_ON_429)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            backoff_sleep(attempt)
    raise RuntimeError(f"Failed to fetch {url}")

def list_date_files(date_folder: str):
    """Single API call to list files in date_folder (non-recursive)."""
    print(f"Listing files in {REPO_ID}:{date_folder}/ ...")
    try:
        items = list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            repo_type="dataset",
            token=HF_TOKEN or None,
        )
    except Exception as exc:
        # If token missing/unauthorized, fallback to public listing via CDN is not possible for tree.
        # We require at least one token-scoped list call per run (per pattern).
        print(f"Failed to list repo tree: {exc}", file=sys.stderr)
        raise
    # items are TreeEntry objects; keep only files
    files = [it for it in items if it.type == "file"]
    print(f"Found {len(files)} files in {date_folder}/")
    return files

def assign_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def parse_to_pair(file_path: str, content: bytes):
    """
    Best-effort projection to {prompt,response}.
    Supports common HF dataset file patterns:
    - JSON/JSONL with 'prompt'/'response' or 'instruction'/'output'
    - Parquet is avoided by design (schema heterogeneity); if encountered,
      skip and log.
    """
    name = Path(file_path).name.lower()
    if name.endswith(".parquet"):
        # Skip parquet files to avoid pyarrow CastError on mixed schemas.
        # Per pattern: download individually and project at parse time.
        # We skip here; if needed, use pyarrow separately with schema coercion.
        return None

    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        return None

    # JSONL: try line-by-line
    if name.endswith(".jsonl"):
        pairs = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
            response = obj.get("response") or obj.get("output")
            if prompt and response:
                pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    # JSON (single object or array)
    if name.endswith(".json"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, list):
            pairs = []
            for item in obj:
                prompt = item.get("prompt") or item.get("instruction") or item.get("input")
                response = item.get("response") or item.get("output")
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
            return pairs
        else:
            prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
            response = obj.get("response") or obj.get("output")
            if prompt and response:
                return [{"prompt": str(prompt), "response": str(response)}]
        return None

    # Plain text fallback: treat whole file as prompt, empty response (skip)
    return None

def slug_for_path(file_path: str) -> str:
    """Deterministic slug for dedup/sharding."""
    return file_path.strip("/")

def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_file = OUTPUT_DIR / f"shard{SHARD_ID}-{ts}.jsonl"


