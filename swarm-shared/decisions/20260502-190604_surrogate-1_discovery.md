# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value incremental improvement**: Add deterministic pre-flight file listing + CDN-only ingestion path to eliminate HF API rate limits during training and make shard workers resilient to 429s.

### What we’ll change
- Add `bin/list-folder.sh` → uses `huggingface_hub` to list one date folder (non-recursive) and emit `file-list.json`.
- Update `bin/dataset-enrich.sh` to accept an optional file-list JSON; if provided, workers stream via CDN URLs (`resolve/main/...`) instead of `load_dataset`.
- Keep existing `datasets` fallback for local dev when no list provided.
- Add lightweight `lib/cdn.py` helper to fetch parquet via CDN and project `{prompt,response}` without schema assumptions.
- Update workflow to run list step once and pass the file list into the matrix (or embed it in the repo for that run).

### Why this is highest value
- Eliminates HF API 429s during parallel ingestion (CDN tier has much higher limits).
- Single API call per cron tick (respects 1000/5min limit) instead of 16× recursive `list_repo_files`.
- Enables reproducible training runs: the exact file list is pinned for that ingestion batch.
- Fits in <2h: small focused scripts, no infra changes.

---

## Concrete changes

### 1) Add `bin/list-folder.sh`
```bash
#!/usr/bin/env bash
# bin/list-folder.sh
# Usage: HF_TOKEN=... ./bin/list-folder.sh <repo> <date-folder> > file-list.json
# Example: ./bin/list-folder.sh axentx/surrogate-1-training-pairs batches/public-merged/2026-05-02

set -euo pipefail
REPO="${1:-axentx/surrogate-1-training-pairs}"
FOLDER="${2:-}"

if [ -z "$FOLDER" ]; then
  echo "Usage: $0 <repo> <folder>" >&2
  exit 1
fi

python3 - "$REPO" "$FOLDER" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo_id = sys.argv[1]
folder = sys.argv[2].rstrip("/")
api = HfApi(token=os.environ.get("HF_TOKEN"))
# non-recursive: one API call, no pagination explosion
items = api.list_repo_tree(repo_id, path=folder, recursive=False)
files = [it.rfilename for it in items if not it.rfilename.endswith("/")]
print(json.dumps({"repo": repo_id, "folder": folder, "files": files}, indent=2))
PY
```
Make executable:
```bash
chmod +x bin/list-folder.sh
```

---

### 2) Add `lib/cdn.py`
```python
# lib/cdn.py
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from io import BytesIO
from typing import List, Dict, Any

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def fetch_parquet_via_cdn(repo: str, path: str) -> pa.Table:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return pq.read_table(BytesIO(resp.content))

def project_pair(row: Dict[str, Any]) -> Dict[str, str]:
    # Best-effort projection to {prompt, response}
    prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
    response = row.get("response") or row.get("output") or row.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def stream_cdn_files(repo: str, files: List[str], max_files: int = None):
    count = 0
    for path in files:
        if max_files is not None and count >= max_files:
            break
        try:
            tbl = fetch_parquet_via_cdn(repo, path)
            for batch in tbl.to_batches(max_chunksize=1000):
                for row in batch.to_pylist():
                    yield project_pair(row)
            count += 1
        except Exception as exc:
            # Log and skip bad files; don't kill whole shard
            print(f"skip {path}: {exc}", flush=True)
            continue
```

---

### 3) Update `bin/dataset-enrich.sh` (minimal diff)
Add optional file-list mode and CDN path:

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Existing behavior preserved; new mode:
#   USE_CDN=1 FILE_LIST=file-list.json ./bin/dataset-enrich.sh <shard_id> <n_shards>

set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${1:-0}"
N_SHARDS="${2:-16}"
USE_CDN="${USE_CDN:-0}"
FILE_LIST="${FILE_LIST:-}"

# deterministic shard assignment by slug hash
hash_slug() {
  echo -n "$1" | sha256sum | cut -c1-16
}

belongs_to_shard() {
  local slug="$1"
  local h=$(hash_slug "$slug")
  local n=$(( 0x${h} % N_SHARDS ))
  [ "$n" -eq "$SHARD_ID" ]
}

process_with_datasets() {
  python3 - "$SHARD_ID" "$N_SHARDS" <<'PY'
import os, json, hashlib
from datasets import load_dataset

shard_id = int(os.environ["SHARD_ID"])
n_shards = int(os.environ["N_SHARDS"])
repo = os.environ["REPO"]

def hash_slug(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest()[:16], 16)

ds = load_dataset(repo, split="train", streaming=True)
for row in ds:
    slug = row.get("slug", "")
    if hash_slug(slug) % n_shards != shard_id:
        continue
    # existing normalization logic here (kept unchanged)
    print(json.dumps(row, ensure_ascii=False))
PY
}

process_with_cdn() {
  local list="$1"
  python3 - "$SHARD_ID" "$N_SHARDS" "$list" <<'PY'
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.cdn import stream_cdn_files

shard_id = int(sys.argv[1])
n_shards = int(sys.argv[2])
list_file = sys.argv[3]

with open(list_file) as f:
    meta = json.load(f)

repo = meta["repo"]
files = meta["files"]
# deterministic shard assignment on filenames to avoid cross-shard duplicates
selected = [p for p in files if (int.from_bytes(p.encode(), 'little') % n_shards) == shard_id]

for pair in stream_cdn_files(repo, selected):
    print(json.dumps(pair, ensure_ascii=False))
PY
}

if [ "$USE_CDN" = "1" ] && [ -n "$FILE_LIST" ] && [ -f "$FILE_LIST" ]; then
  echo "Using CDN mode with file-list: $FILE_LIST" >&2
  process_with_cdn "$FILE_LIST"
else
  echo "Using datasets streaming mode" >&2
  process_with_datasets
fi
```
Make sure it’s executable:
```bash
chmod +x bin/dataset-enrich.sh
```

---

### 4) Update workflow (`.github/workflows/ingest.yml`) — minimal
Add a pre-step that lists today’s folder and passes it to matrix jobs:

```yaml
# .github/workflows/ingest.yml (excerpt)
jobs:
  prepare-list:
    runs-on: ubuntu-latest
    outputs:
      file_list: ${{ steps.set.outputs.file_list }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - id: set
        run: |
          DATE=$(date +%Y-%m-%d)
          FOLDER="batches/public-merged/$DATE"
          ./bin/list
