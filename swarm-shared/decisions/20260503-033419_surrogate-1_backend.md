# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE` (defaults to today UTC `YYYY-MM-DD`)
- Uses **one API call** from the runner to `list_repo_tree(recursive=False)` for `public-merged/{DATE}/`, saves `file-list.json`, then performs **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to bypass HF API 429 limits
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError from mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Exits non-zero on fatal errors; logs progress to stdout for GitHub Actions

### Why this is the highest-value incremental improvement
- Directly applies the **HF CDN bypass** insight (eliminates 429s during parallel ingestion)
- Eliminates `load_dataset(streaming=True)` on heterogeneous repos (fixes pyarrow CastError)
- Keeps the 16-shard parallel architecture but makes each worker independent and robust
- Fits within <2h: single-file replacement, minimal refactor, reuses existing dedup and workflow

---

## Code Snippets

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID          (required) 0..15
  SHARD_TOTAL       (default 16)
  DATE              (default today UTC YYYY-MM-DD)
  HF_TOKEN          write token for axentx/surrogate-1-training-pairs
  REPO_ID           (default axentx/surrogate-1-training-pairs)
"""

import os
import sys
import json
import hashlib
import logging
import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")

# ---------- config ----------
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "-1"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    log.error("SHARD_ID must be in [0, SHARD_TOTAL-1]")
    sys.exit(1)

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# ---------- helpers ----------
def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode("utf-8")).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date: str) -> List[str]:
    """
    Single API call: list top-level files in /public-merged/{date}/
    Returns relative paths within repo.
    """
    folder = f"public-merged/{date}"
    log.info("Listing repo tree: %s/%s", REPO_ID, folder)
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=folder, recursive=False)
    except Exception as exc:
        log.exception("Failed to list repo tree")
        raise RuntimeError(f"list_repo_tree failed: {exc}") from exc

    files = [item.rfilename for item in tree if item.type == "file"]
    log.info("Found %d files in %s", len(files), folder)
    return files

def cdn_url(repo_id: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

def download_via_cdn(path: str, local_path: Path) -> None:
    url = cdn_url(REPO_ID, path)
    log.info("Downloading via CDN: %s", url)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)

def parse_to_pair(raw_bytes: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project arbitrary file to {prompt, response} at parse time.
    Supports:
      - JSONL with 'prompt'/'response' or 'instruction'/'output'
      - JSON list of objects
      - Parquet (via temporary file + pyarrow) — project only required cols
    """
    import pyarrow.parquet as pq
    from io import BytesIO

    pairs = []
    name = filename.lower()

    try:
        if name.endswith(".jsonl"):
            for line in raw_bytes.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("instruction") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    pairs.append({"prompt": prompt, "response": response})

        elif name.endswith(".json"):
            obj = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(obj, list):
                items = obj
            else:
                items = [obj]
            for item in items:
                prompt = item.get("prompt") or item.get("instruction") or ""
                response = item.get("response") or item.get("output") or ""
                if prompt and response:
                    pairs.append({"prompt": prompt, "response": response})

        elif name.endswith(".parquet"):
            table = pq.read_table(BytesIO(raw_bytes))
            cols = set(table.column_names)
            prompt_col = next((c for c in ("prompt", "instruction") if c in cols), None)
            response_col = next((c for c in ("response", "output") if c in cols), None)
            if prompt_col and response_col:
                for i in range(table.num_rows):
                    prompt = str(table[prompt_col][i].as_py())
                    response = str(table[response_col][i].as_py())
                    if prompt.strip() and response.strip():
                        pairs.append({"prompt": prompt, "response": response})
        else:
            log.warning("Unsupported file type: %s", filename)
    except Exception as exc:
        log.warning("Failed to parse %s: %s", filename, exc)

    return pairs

# ---------- dedup ----------
from lib.dedup import DedupStore

def main() -> None:
    log.info("Starting worker: shard=%d/%d date=%s", SHARD_ID, SHARD_TOTAL, DATE)

    # list files once
    files = list_date_files(DATE)
    assigned = [f for f in files if deterministic_shard(f) == SHARD_ID]
    log.info("Assigned %d files for shard %d", len(assigned), SHARD_ID)

    out_dir = Path(f"batches/public-merged/{DATE}")
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    dedup = DedupStore()
    written = 0
    skipped_dup = 0
    failed_files = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for rel_path in assigned:
            try:
                tmp = Path("tmp") / rel_path.replace("/", "_")
                tmp.parent.mkdir(parents=True, exist_ok=True)

                download_via_cdn(rel_path, tmp)
                pairs = parse_to_pair(tmp.read_bytes(), rel_path)

                for pair in pairs:

