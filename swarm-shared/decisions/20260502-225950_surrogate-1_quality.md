# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes
1. Add `bin/list-date-files.py` — single Mac-side script that calls `list_repo_tree(recursive=False)` for a date folder on `axentx/surrogate-1-training-pairs`, saves `date-files.json` (flat list of file paths under that date). Embed this list into training and worker scripts so they do **zero** `list_repo_files`/`load_dataset` API calls during data loading.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON and stream files via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`). Keep existing HF write path for outputs unchanged.
3. Add `bin/train-cdn.py` skeleton showing how Lightning training uses the embedded file list and CDN-only fetches (no HF dataset streaming) to avoid 429s.
4. Add retry/backoff for CDN downloads (separate from API rate limits) and respect HF CDN caching headers.

### Why this is highest value
- Eliminates the primary cause of 429s during ingestion/training (recursive `list_repo_files` and `load_dataset` API calls).
- Makes shard workers independent of HF API availability after the initial file-list snapshot.
- Fits existing layout and requires no schema changes.
- Can ship in <2h with minimal risk.

---

## Code snippets

### 1) bin/list-date-files.py
```python
#!/usr/bin/env python3
"""
Generate a deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/list-date-files.py --date 2026-05-02 --out date-files.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser(description="List date folder files (non-recursive).")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--repo", default=REPO_ID, help="HF dataset repo")
    args = parser.parse_args()

    # Validate date format
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("Error: --date must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    folder_path = args.date  # top-level date folder

    # Use recursive=False to avoid paginating huge trees.
    # We only want the immediate files in this date folder.
    entries = api.list_repo_tree(repo_id=args.repo, path=folder_path, recursive=False)

    files = []
    for e in entries:
        # Only include files (ignore subfolders)
        if e.type == "file":
            files.append(f"{folder_path}/{e.path}")

    # Deterministic ordering
    files.sort()

    out_data = {
        "date": args.date,
        "repo": args.repo,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-date-files.py
```

---

### 2) bin/dataset-enrich.sh (updated)
Add CDN streaming option and optional file-list input. Keep existing behavior as default.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Normalize + dedup + upload shard outputs.
#
# New: If DATE_FILES_JSON is provided, stream via CDN instead of HF datasets API.

set -euo pipefail
# Ensure consistent shell for cron/actions
export SHELL=/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../" && pwd)"

# Config
HF_REPO="datasets/axentx/surrogate-1-training-pairs"
DATE=$(date -u +"%Y-%m-%d")
TS=$(date -u +"%Y%m%d%H%M%S")
SHARD_ID="${SHARD_ID:-0}"
HF_TOKEN="${HF_TOKEN:-}"
DATE_FILES_JSON="${DATE_FILES_JSON:-}"  # optional: pre-generated file list
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/shard-out}"
mkdir -p "${OUTPUT_DIR}"

OUTFILE="${OUTPUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

# Dedup store (central)
DEDUP_STORE="${SCRIPT_DIR}/lib/dedup.py"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

# ---- helpers ----

stream_via_cdn() {
  # Expects DATE_FILES_JSON with { "files": [ "YYYY-MM-DD/file.parquet", ... ] }
  if [[ -z "${DATE_FILES_JSON}" || ! -f "${DATE_FILES_JSON}" ]]; then
    log "DATE_FILES_JSON not set or missing, skipping CDN mode"
    return 1
  fi

  python3 -c "
import json, sys, os, subprocess, tempfile, shutil, hashlib, time, random
from urllib.request import urlopen, Request
from urllib.error import HTTPError

HF_REPO = '${HF_REPO}'
DATE = '${DATE}'
OUTFILE = '${OUTFILE}'
DEDUP_STORE = '${DEDUP_STORE}'
DATE_FILES_JSON = '${DATE_FILES_JSON}'

with open(DATE_FILES_JSON) as f:
    manifest = json.load(f)

files = manifest.get('files', [])
if not files:
    print('No files in manifest')
    sys.exit(0)

# Deterministic shard assignment: hash basename -> mod 16
def shard_for(path):
    return hashlib.md5(os.path.basename(path).encode()).hexdigest()[0], int(hashlib.md5(os.path.basename(path).encode()).hexdigest(), 16) % 16

target_shard = int(os.environ.get('SHARD_ID', 0))

def download_cdn(url, retries=5):
    for attempt in range(retries):
        try:
            req = Request(url, headers={'User-Agent': 'axentx-surrogate-1'})
            with urlopen(req, timeout=30) as resp:
                return resp.read()
        except HTTPError as e:
            if e.code == 404:
                print(f'Not found (skip): {url}')
                return None
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f'HTTP error {e.code} on {url}, retry {attempt+1}/{retries} in {wait:.1f}s')
            time.sleep(wait)
        except Exception as e:
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f'Error on {url}: {e}, retry {attempt+1}/{retries} in {wait:.1f}s')
            time.sleep(wait)
    print(f'Failed after {retries} retries: {url}')
    return None

import pyarrow.parquet as pq
import pyarrow as pa
import io

out_f = open(OUTFILE, 'w', encoding='utf-8')
processed = 0

for rel in files:
    # shard assignment
    _, s = shard_for(rel)
    if s != target_shard:
        continue

    cdn_url = f'https://huggingface.co/datasets/{HF_REPO}/resolve/main/{rel}'
    data = download_cdn(cdn_url)
    if data is None:
        continue

    try:
        table = pq.read_table(io.BytesIO(data))
        # Project to prompt/response only (schema resilience)
        cols = table.column_names
        prompt_col = next((c for c in ('prompt', 'instruction', 'input') if c in cols), None)
        response_col
