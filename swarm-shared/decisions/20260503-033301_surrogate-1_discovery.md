# surrogate-1 / discovery

Below is the **single, merged implementation** that keeps the strongest, most actionable parts of both proposals, removes contradictions, and favors correctness + deployability within the ≤2h budget.

---

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE_FOLDER` (default today), `HF_TOKEN` (required for listing)
- Uses **one API call** (`list_repo_tree(recursive=False)`) to list files in `{DATE_FOLDER}/`
- Shards files **deterministically by path** (`hash(path) % SHARD_TOTAL`) so matrix workers never overlap
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no auth header during data fetch**
- Projects each record to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` on mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success; logs summary (files, pairs, dups, bytes)

Why this is highest-value:
- Directly applies **HF CDN bypass** and **manifest pre-list** patterns to eliminate 429s during training data load.
- Replaces brittle shell script with typed Python that handles schema heterogeneity and retries safely.
- Minimal refactor: single-file replacement, reuses existing dedup lib and workflow matrix.

---

## File Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-04-29 python bin/dataset-enrich.py

Environment:
  HF_TOKEN          - write token for axentx/surrogate-1-training-pairs (required for listing)
  SHARD_ID          - 0..15 (required)
  SHARD_TOTAL       - default 16
  DATE_FOLDER       - YYYY-MM-DD folder under dataset repo (default today)
"""
import os
import sys
import json
import hashlib
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import requests
from huggingface_hub import HfApi

# Project-local dedup
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")

REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

# ----------------------------
# Schema adapters
# ----------------------------
def project_to_pair(raw: Dict[str, Any], filename: str) -> Dict[str, str]:
    """
    Heuristic projection to {prompt, response}.
    Extend per observed schema.
    """
    # Common patterns seen in surrogate-1
    if "prompt" in raw and "response" in raw:
        return {"prompt": str(raw["prompt"]), "response": str(raw["response"])}
    if "input" in raw and "output" in raw:
        return {"prompt": str(raw["input"]), "response": str(raw["output"])}
    if "question" in raw and "answer" in raw:
        return {"prompt": str(raw["question"]), "response": str(raw["answer"])}

    # Fallback: pick first two text-like fields
    text_keys = [k for k, v in raw.items() if isinstance(v, str) and len(v) > 10]
    if len(text_keys) >= 2:
        return {"prompt": text_keys[0], "response": text_keys[1]}

    raise ValueError(f"Cannot project {filename}: {list(raw.keys())}")

# ----------------------------
# Sharding / listing
# ----------------------------
def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % total

def list_date_files(api: HfApi, date_folder: str) -> List[str]:
    """
    Single API call: list files in date folder (non-recursive).
    """
    log.info("Listing files for %s", date_folder)
    tree = api.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]
    log.info("Found %d files", len(files))
    return files

# ----------------------------
# CDN-bypass download
# ----------------------------
def download_via_cdn(path_in_repo: str, local_path: Path) -> None:
    """
    CDN bypass download (no auth header). Retries with exponential backoff.
    """
    url = f"{BASE_CDN}/{path_in_repo}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            return
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("Download attempt %s failed: %s — retry in %ss", attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url}")

# ----------------------------
# Parsers
# ----------------------------
def safe_load_parquet(path: Path):
    import pyarrow.parquet as pq
    try:
        return pq.read_table(path).to_pylist()
    except Exception as exc:
        log.warning("Parquet read failed %s: %s", path, exc)
        raise

def safe_load_jsonl(path: Path):
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception as exc:
                log.warning("Invalid JSON line in %s: %s", path, exc)
    return rows

def load_file(path: Path):
    if path.suffix == ".parquet":
        return safe_load_parquet(path)
    if path.suffix == ".jsonl":
        return safe_load_jsonl(path)
    if path.suffix == ".json":
        with path.open() as f:
            return json.load(f)
    raise ValueError(f"Unsupported file type: {path}")

# ----------------------------
# Main
# ----------------------------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard_id", type=int, required=True)
    parser.add_argument("--shard_total", type=int, default=16)
    parser.add_argument("--date_folder", type=str, default=None)
    parser.add_argument("--hf_token", type=str, required=True)
    args = parser.parse_args()

    SHARD_ID = args.shard_id
    SHARD_TOTAL = args.shard_total
    DATE_FOLDER = args.date_folder or datetime.utcnow().strftime("%Y-%m-%d")
    HF_TOKEN = args.hf_token

    if not (0 <= SHARD_ID < SHARD_TOTAL):
        log.error("Invalid SHARD_ID/SHARD_TOTAL")
        sys.exit(1)

    api = HfApi(token=HF_TOKEN)
    dedup = DedupStore()

    # List and shard
    files = list_date_files(api, DATE_FOLDER)
    assigned_files = [
        f for f in files
        if deterministic_shard(f, SHARD_TOTAL) == SHARD_ID
    ]
    assigned_files.sort()
    log.info("Assigned %d files for shard %d/%d", len(assigned_files), SHARD_ID, SHARD_TOTAL)

    # Output path
    out_dir = Path("batches/public-merged") / DATE_FOLDER
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.json
