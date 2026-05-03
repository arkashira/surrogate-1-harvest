# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list saved to `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` 429 limits)
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with slug-only attribution in filename (no `source`/`ts` columns)
- Returns exit code 0 on success, non-zero on fatal failure (GitHub Actions will retry)

### Why this is the highest-value incremental improvement
- Eliminates HF API rate-limit (429) risk during parallel ingestion by using CDN-only fetches
- Avoids `load_dataset(streaming=True)` schema heterogeneity crashes
- Keeps 16-shard parallelism while respecting HF commit cap (128/hr) via deterministic sharding
- Fits <2h: single-file replacement, minimal refactor, reuses existing dedup and workflow

---

## Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
    python bin/dataset-enrich.py

Environment:
  HF_TOKEN          - HuggingFace write token (for dedup store + final push)
  SHARD_ID          - 0..15
  SHARD_TOTAL       - default 16
  DATE              - folder in axentx/surrogate-1-training-pairs
  REPO_ID           - default axentx/surrogate-1-training-pairs
  MANIFEST_PATH     - optional path to pre-saved manifest JSON
"""

import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx  # single session, connection reuse
from huggingface_hub import HfApi, hf_hub_download

# ── config --
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()
HF_TOKEN = os.getenv("HF_TOKEN", "")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "")

if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

if not (0 <= SHARD_ID < SHARD_TOTAL):
    print(f"ERROR: SHARD_ID must be 0..{SHARD_TOTAL - 1}", file=sys.stderr)
    sys.exit(1)

# ── paths --
BASE_DIR = Path(__file__).parent.parent
LIB_DIR = BASE_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

try:
    from dedup import DedupStore
except ImportError as e:
    print(f"ERROR: cannot import dedup: {e}", file=sys.stderr)
    sys.exit(1)

OUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── hf clients --
api = HfApi(token=HF_TOKEN)
dedup = DedupStore()

# ── helpers --
def slug_for_path(path: str) -> str:
    """Stable slug for dedup/sharding. Remove extension."""
    stem = Path(path).stem
    return stem

def deterministic_shard(slug: str) -> int:
    h = int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16)
    return h % SHARD_TOTAL

def build_manifest() -> List[str]:
    """Single API call: list top-level files in DATE folder."""
    if MANIFEST_PATH and Path(MANIFEST_PATH).exists():
        with open(MANIFEST_PATH) as f:
            data = json.load(f)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str) and p.strip()]

    # list_repo_tree per folder (non-recursive) to avoid 100x pagination
    items = api.list_repo_tree(repo_id=REPO_ID, path=DATE, recursive=False)
    files = []
    for item in items:
        if item.type == "file":
            files.append(f"{DATE}/{item.path}")

    # save for reuse/debug
    manifest_out = Path(f"manifest-{DATE}.json")
    manifest_out.write_text(json.dumps(files, indent=2))
    return files

def safe_project_to_pair(raw_obj: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous HF dataset objects to {prompt, response}.
    Handles common variants without assuming schema.
    """
    # Common field names (case-insensitive-ish)
    text_keys = [k for k in raw_obj.keys() if isinstance(k, str)]
    prompt_candidates = [k for k in text_keys if "prompt" in k.lower()]
    response_candidates = [k for k in text_keys if "response" in k.lower() or "completion" in k.lower() or "answer" in k.lower()]

    prompt_val = None
    response_val = None

    if prompt_candidates:
        prompt_val = str(raw_obj[prompt_candidates[0]]).strip()
    if response_candidates:
        response_val = str(raw_obj[response_candidates[0]]).strip()

    # Fallback: if object has exactly two text fields, assign by length
    text_fields = [(k, str(v).strip()) for k, v in raw_obj.items() if isinstance(v, str) and v.strip()]
    if not prompt_val and not response_val and len(text_fields) == 2:
        a, b = text_fields[0][1], text_fields[1][1]
        if len(a) >= len(b):
            prompt_val, response_val = b, a
        else:
            prompt_val, response_val = a, b

    # Final fallback
    if not prompt_val:
        prompt_val = ""
    if not response_val:
        response_val = ""

    return {"prompt": prompt_val, "response": response_val}

def download_via_cdn(repo_id: str, repo_path: str) -> bytes:
    """
    CDN bypass: https://huggingface.co/datasets/{repo}/resolve/main/{path}
    No Authorization header -> avoids /api/ rate limits.
    """
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{repo_path}"
    # Use httpx for timeout/retry control
    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    return resp.content

def parse_parquet(content: bytes) -> List[Dict[str, str]]:
    import pyarrow.parquet as pq
    import io
    table = pq.read_table(io.BytesIO(content))
    rows = []
    for i in range(table.num_rows):
        raw = {col: table[col][i].as_py() for col in table.column_names}
        rows.append(safe_project_to_pair(raw))
    return rows

def parse_jsonl(content: bytes) -> List[Dict[str, str]]:
    rows = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        rows.append(safe_project_to_pair(obj))
    return rows

def parse
