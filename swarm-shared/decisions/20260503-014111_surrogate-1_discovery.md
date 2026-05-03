# surrogate-1 / discovery

## Implementation Plan — Manifest-Driven CDN-Bypass Ingestion Pipeline

**Goal**: Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion pipeline that eliminates HF API rate limits (429) and mixed-schema `pyarrow` errors, and produces clean `{prompt,response}` pairs into `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

**Scope**: Single high-value change that can ship in <2h and unblocks stable, scalable ingestion for surrogate-1.

---

### 1) High-level flow (what changes)

1. **Mac orchestrator** (run manually or via cron):
   - Calls HF API **once** per date folder with `list_repo_tree(path, recursive=False)` (non-recursive to avoid pagination explosion).
   - Saves file list + metadata to `manifests/<date>/file-list.json`.
   - Commits/pushes manifest (optional) or passes to Actions via `workflow_dispatch` input.

2. **GitHub Actions matrix (16 shards)**:
   - Each runner receives:
     - `DATE` (e.g., `2026-05-03`)
     - `SHARD_ID` (0–15)
     - `FILE_LIST` path or inline JSON
   - Runner **does not call HF API** during ingestion. It only uses **CDN URLs**:
     - `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>`
   - Per file:
     - Download via CDN (no auth, bypasses `/api/` rate limits).
     - Parse safely:
       - If parquet: read with `pyarrow` and project only `{prompt, response}` at parse time; ignore extra columns.
       - If JSON/JSONL: stream and extract `{prompt, response}`.
       - If schema is missing/heterogeneous: skip malformed rows and log.
     - Compute deterministic `md5` over normalized content for dedup (central SQLite store on HF Space remains source of truth; local dedup is best-effort per-run).
   - Output: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` (one line per `{prompt, response}`).
   - Push to HF dataset repo using `huggingface_hub` with `HF_TOKEN`.

3. **Deterministic shard assignment**:
   - `shard_id = hash(slug) % 16`
   - Ensures same file always maps to same shard across runs → no cross-shard collisions.

4. **Idempotency & collision avoidance**:
   - Filename includes `shard<N>-<HHMMSS>` → unique per run.
   - Central dedup store on HF Space prevents duplicates across runs (best-effort; wasted bandwidth acceptable per trade-offs).

---

### 2) Concrete file changes

#### New: `bin/build-manifest.py`
```python
#!/usr/bin/env python3
"""
Usage: python bin/build-manifest.py --date 2026-05-03 --out manifests/2026-05-03/file-list.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi

API = HfApi()
REPO = "datasets/axentx/surrogate-1-training-pairs"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder in repo (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    # Non-recursive to avoid pagination explosion
    entries = API.list_repo_tree(REPO, path=args.date, recursive=False)
    files = [e.path for e in entries if e.type == "file"]

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"date": args.date, "files": files}, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```
- Make executable: `chmod +x bin/build-manifest.py`.

---

#### Replace: `bin/dataset-enrich.sh` → `bin/dataset-enrich.py`

Rationale: Bash is fragile for JSON, retries, and per-row schema projection. Python gives safe, readable, and maintainable parsing with CDN bypass.

```python
#!/usr/bin/env python3
"""
Per-shard CDN-bypass ingestion worker.

Environment:
  DATE=2026-05-03
  SHARD_ID=0..15
  FILE_LIST=manifests/2026-05-03/file-list.json  (or inline JSON)
  HF_TOKEN=...
  OUTPUT_DIR=batches/public-merged
"""
import json
import os
import sys
import hashlib
import time
import logging
from pathlib import Path
from datetime import datetime

import pyarrow.parquet as pq
import pyarrow as pa
import requests
from huggingface_hub import HfApi, hf_hub_download

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REPO = "datasets/axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"

API = HfApi()

def deterministic_shard(slug: str, n_shards: int = 16) -> int:
    return hash(slug) % n_shards

def normalize_pair(obj) -> dict:
    # Accept multiple plausible key names; project to {prompt, response}
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def compute_hash(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode("utf-8")).hexdigest()

def parse_parquet_cdn(path: str):
    url = f"{BASE_CDN}/{path}"
    local_path = hf_hub_download(repo_id=REPO, filename=path, cache_dir="/tmp/hf_cache")
    try:
        table = pq.read_table(local_path, columns=["prompt", "response"], use_threads=False)
    except (pa.ArrowInvalid, KeyError, OSError):
        # Fallback: read all and project
        try:
            table = pq.read_table(local_path, use_threads=False)
        except Exception as e:
            logging.warning("Failed to read parquet %s: %s", path, e)
            return
    df = table.to_pandas()
    for _, row in df.iterrows():
        pair = normalize_pair(row.to_dict())
        if pair["prompt"] and pair["response"]:
            yield pair

def parse_jsonlines_cdn(path: str):
    url = f"{BASE_CDN}/{path}"
    r = requests.get(url, stream=True, timeout=30)
    r.raise_for_status()
    for line in r.iter_lines(decode_unicode=True):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        pair = normalize_pair(obj)
        if pair["prompt"] and pair["response"]:
            yield pair

def parse_file(path: str):
    if path.endswith(".parquet"):
        yield from parse_parquet_cdn(path)
    elif path.endswith(".jsonl") or path.endswith(".json"):
        yield from parse_jsonlines_cdn(path)
    else:
        logging.info("Skipping unsupported file: %s", path)

def main():
    date = os.getenv("DATE")
    shard_id = int(os.getenv("SHARD_ID", "0"))
    file_list_src = os.getenv("FILE_LIST")
    if not date or not file_list_src:
        logging.error("DATE and FILE_LIST env required")
        sys.exit(1)

    # Load file list
    if os.path.isfile(file_list_src):
        with open(file_list_src) as f:
            manifest = json.load(f)
    else:
        # inline JSON fallback
        manifest = json.loads(file_list_src)

    files = [f for f in manifest.get("files", []) if f.startswith(date)]
    logging.info("Processing %d files for
