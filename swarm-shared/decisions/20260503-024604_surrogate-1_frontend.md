# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (write)  
- Single `list_repo_tree(path, recursive=False)` for `{DATE}/` → deterministic shard assignment by `hash(slug) % SHARD_TOTAL`  
- Downloads only assigned files via **CDN bypass** (`resolve/main/...`, no Authorization header)  
- Projects heterogeneous HF datasets to `{prompt, response}` at parse time (no `load_dataset(streaming=True)` on mixed schemas)  
- Deduplicates via central `lib/dedup.py` md5 store  
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with HF write token  
- Exits non-zero on fatal errors; logs structured JSON for GH Actions

---

## Code

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          int   0..SHARD_TOTAL-1
  SHARD_TOTAL       int   default 16
  DATE              str   YYYY-MM-DD folder under dataset repo
  HF_TOKEN          str   write token for axentx/surrogate-1-training-pairs
  DATASET_REPO      str   default axentx/surrogate-1-training-pairs
  LOG_LEVEL         str   default INFO
"""

import json
import logging
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ---- config ----
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "-1"))
DATE = os.getenv("DATE", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ---- logging ----
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("surrogate-ingest")

# ---- validation ----
def fatal(msg: str) -> None:
    log.error(msg)
    sys.exit(1)

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    fatal(f"Invalid SHARD_ID={SHARD_ID} for SHARD_TOTAL={SHARD_TOTAL}")
if not DATE:
    fatal("DATE must be set (YYYY-MM-DD)")
if not HF_TOKEN:
    fatal("HF_TOKEN must be set")

# ---- constants ----
API = HfApi(token=HF_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_ROOT = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main"
DATE_PREFIX = f"{DATE}/"
OUTPUT_DIR = Path("batches/public-merged") / DATE
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ---- dedup ----
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ---- helpers ----
def slug_from_path(path: str) -> str:
    """
    Convert repo tree path to stable slug.
    Examples:
      2026-04-29/abc.parquet -> 2026-04-29/abc
      2026-04-29/sub/xyz.jsonl -> 2026-04-29/sub/xyz
    """
    p = Path(path)
    return str(p.with_suffix(""))

def shard_for_slug(slug: str) -> int:
    h = int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16)
    return h % SHARD_TOTAL

def cdn_url(path: str) -> str:
    return f"{CDN_ROOT}/{path}"

def is_parquet(path: str) -> bool:
    return path.lower().endswith(".parquet")

def is_jsonl(path: str) -> bool:
    return path.lower().endswith(".jsonl")

def is_json(path: str) -> bool:
    return path.lower().endswith(".json")

# ---- parsers (project to {prompt,response}) ----
def try_parquet(content: bytes) -> List[Dict[str, str]]:
    import pyarrow as pa
    import pyarrow.parquet as pq
    tbl = pq.read_table(pa.BufferReader(content))
    rows = []
    cols = tbl.column_names
    # heuristic: find prompt/response or text/completion
    prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
    response_col = next((c for c in cols if "response" in c.lower() or "completion" in c.lower()), None)
    if prompt_col and response_col:
        for i in range(tbl.num_rows):
            rows.append({
                "prompt": str(tbl[prompt_col][i].as_py()),
                "response": str(tbl[response_col][i].as_py()),
            })
        return rows
    # fallback: try to find any two text-ish columns
    text_cols = [c for c in cols if tbl.schema.field(c).type in (pa.string(), pa.large_string())]
    if len(text_cols) >= 2:
        for i in range(tbl.num_rows):
            rows.append({
                "prompt": str(tbl[text_cols[0]][i].as_py()),
                "response": str(tbl[text_cols[1]][i].as_py()),
            })
        return rows
    # last resort: serialize row as prompt, empty response
    for i in range(tbl.num_rows):
        rows.append({
            "prompt": json.dumps(tbl.slice(i, 1).to_pydict()),
            "response": "",
        })
    return rows

def try_jsonl_lines(content: bytes) -> List[Dict[str, str]]:
    rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get("prompt") or obj.get("text") or obj.get("input") or ""
        response = obj.get("response") or obj.get("completion") or obj.get("output") or ""
        rows.append({"prompt": str(prompt), "response": str(response)})
    return rows

def try_json(content: bytes) -> List[Dict[str, str]]:
    try:
        obj = json.loads(content)
    except Exception:
        return []
    if isinstance(obj, list):
        rows = []
        for item in obj:
            if isinstance(item, dict):
                prompt = item.get("prompt") or item.get("text") or item.get("input") or ""
                response = item.get("response") or item.get("completion") or item.get("output") or ""
                rows.append({"prompt": str(prompt), "response": str(response)})
        return rows
    if isinstance(obj, dict):
        prompt = obj.get("prompt") or obj.get("text") or obj.get("input") or ""
        response = obj.get("response") or obj.get("completion") or obj.get("output") or ""
        return [{"prompt": str(prompt), "response": str(response)}]
    return []

def parse_file(path: str, content: bytes) -> List[Dict[str, str]]:
    if is_parquet(path):
        return try_parquet(content)
    if is_jsonl(path):
        return try_jsonl_lines(content)
