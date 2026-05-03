# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Pre-lists target folder once via `list_repo_tree(path=DATE, recursive=False)` → saves `manifest.json`
- Each shard deterministically hashes `slug` → picks assigned shard; only processes its slice
- Downloads files via **CDN bypass** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header to avoid HF API 429
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deduplicates via central md5 store (`lib/dedup.py`) and SQLite as source-of-truth for cross-run dedup
- Writes `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl` with deterministic filename (shard + ts)
- Exits 0 on success; logs counts and skips; retries CDN downloads with backoff; returns non-zero exit code on failure

### Code changes

`bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py

Environment:
  SHARD_ID          (required) 0..15
  SHARD_TOTAL       (default 16)
  DATE              (required) YYYY-MM-DD folder under dataset
  HF_TOKEN          (required) write token
  REPO_ID           (default axentx/surrogate-1-training-pairs)
  MANIFEST_PATH     (optional) path to pre-saved manifest.json
"""

import os
import sys
import json
import hashlib
import time
import logging
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dataset-enrich")

# ---------- config ----------
SHARD_ID = int(os.getenv("SHARD_ID", ""))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "")

if not (0 <= SHARD_ID < SHARD_TOTAL):
    log.error("Invalid SHARD_ID or SHARD_TOTAL")
    sys.exit(1)
if not DATE:
    log.error("DATE is required (YYYY-MM-DD)")
    sys.exit(1)
if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
DATE_FOLDER = DATE  # top-level folder in dataset repo
OUTPUT_DIR = Path("batches/public-merged") / DATE
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TS = time.strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"

# ---------- dedup ----------
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from lib.dedup import DedupStore
except Exception as exc:
    log.error("Could not import lib.dedup: %s", exc)
    sys.exit(1)

dedup = DedupStore()

# ---------- helpers ----------
def deterministic_shard(slug: str) -> int:
    h = hashlib.md5(slug.encode("utf-8")).hexdigest()
    return int(h, 16) % SHARD_TOTAL

def list_manifest() -> List[str]:
    """List files in DATE_FOLDER via repo_tree (non-recursive)."""
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        log.info("Using pre-saved manifest: %s", MANIFEST_PATH)
        return json.loads(Path(MANIFEST_PATH).read_text().strip())

    log.info("Listing repo tree: %s @ %s", REPO_ID, DATE_FOLDER)
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=DATE_FOLDER, recursive=False)
    except Exception as exc:
        log.error("list_repo_tree failed: %s", exc)
        raise

    paths = [item.path for item in tree if item.type == "file"]
    if MANIFEST_PATH:
        Path(MANIFEST_PATH).write_text(json.dumps(paths) + "\n")
        log.info("Saved manifest to %s (%d entries)", MANIFEST_PATH, len(paths))
    return paths

def cdn_download(repo_id: str, path: str, retries: int = 3, backoff: float = 2.0) -> bytes:
    """Download via CDN bypass (no auth header)."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            # non-200: retry (some CDN edges may 404 transiently for newly uploaded files)
            log.warning("CDN %s returned %s (attempt %s/%s)", url, resp.status_code, attempt, retries)
        except Exception as exc:
            log.warning("CDN download error %s (attempt %s/%s): %s", url, attempt, retries, exc)

        if attempt < retries:
            sleep_sec = backoff * (2 ** (attempt - 1))
            time.sleep(sleep_sec)
    raise RuntimeError(f"Failed to download {url} after {retries} attempts")

def normalize_json_pair(obj: Dict[str, Any], path: str) -> Dict[str, str]:
    """Project heterogeneous JSON schemas to {prompt, response}."""
    # Common field name variants
    prompt_candidates = [k for k in obj.keys() if "prompt" in k.lower()]
    response_candidates = [k for k in obj.keys() if "response" in k.lower() or "completion" in k.lower()]

    if prompt_candidates and response_candidates:
        prompt = str(obj[prompt_candidates[0]]).strip()
        response = str(obj[response_candidates[0]]).strip()
        return {"prompt": prompt, "response": response}

    # Fallback: first two string fields
    str_items = [(k, str(v).strip()) for k, v in obj.items() if isinstance(v, (str, int, float, bool))]
    if len(str_items) >= 2:
        return {"prompt": str_items[0][1], "response": str_items[1][1]}

    raise ValueError(f"Cannot project JSON pair from {path}: {list(obj.keys())}")

def parse_to_pair(raw: bytes, path: str) -> Dict[str, str]:
    """
    Project heterogeneous file schemas to {prompt, response}.
    Supports: .jsonl, .json, .parquet (via pyarrow), .txt (heuristic).
    """
    import io
    suffix = Path(path).suffix.lower()

    # Parquet: read with pyarrow, pick first row-like projection
    if suffix == ".parquet":
        import pyarrow.parquet as pq
        table = pq.read_table(io.BytesIO(raw))
        cols = table.column_names
        if not cols:
            raise ValueError(f"Empty parquet in {path}")
        prompt_col = next((c for c in cols if "prompt" in c.lower()), cols[0])
        response_col = next((c for c in cols if "response" in c.lower() or "completion" in c.lower()), cols[1] if len(cols) > 1 else cols[0])
        prompt = str(table.column(prompt_col)[0].as_py())
        response = str(table.column(response_col)[0].as_py())
