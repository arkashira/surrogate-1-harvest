# surrogate-1 / frontend

## Implementation Plan (≤2 h)

**Goal**: Eliminate HF API 429s and make shard workers deterministic + CDN-only.

### Changes (3 files)

1. `bin/list-public-files.py` (new)  
   - Single API call to `list_repo_tree(recursive=False)` for today’s folder (or latest available).  
   - Emits `public-files.json` with `{ "date": "...", "files": [...] }`.  
   - Exits 0 if nothing new (skip ingestion).

2. `bin/dataset-enrich.sh` (modify)  
   - Accept optional `FILE_LIST` env var (path to `public-files.json`).  
   - If provided, shard workers read the pre-computed list and download via CDN URLs (`resolve/main/...`).  
   - Keep existing HF dataset streaming path as fallback (for backward compatibility).  
   - Deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`.

3. `.github/workflows/ingest.yml` (modify)  
   - Add a pre-step job that runs `list-public-files.py` and uploads `public-files.json` as an artifact.  
   - Pass artifact path to each matrix shard via `env.FILE_LIST`.  
   - If pre-step produces no new files, skip the matrix entirely (exit 0).

### Why this works
- One API call per cron tick (not per shard) → avoids 429.  
- Shards use CDN downloads (no auth, no rate limit) → safe parallelism.  
- Deterministic shard assignment prevents collisions and enables retries.  
- Backward-compatible: existing Space workers still work unchanged.

---

## Code Snippets

### 1) `bin/list-public-files.py`

```python
#!/usr/bin/env python3
"""
Pre-flight list of public files for today (or latest available).
Usage:
  python list-public-files.py [YYYY-MM-DD] > public-files.json
"""
import json
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"
api = HfApi()

def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = f"batches/public-raw/{date_str}/"

    try:
        tree = api.list_repo_tree(REPO, path=folder, recursive=False)
    except Exception as e:
        # If folder missing, try previous day (graceful fallback)
        sys.stderr.write(f"Folder {folder} not found: {e}\n")
        sys.exit(1)

    files = [entry.rfilename for entry in tree if entry.rfilename.endswith((".parquet", ".jsonl"))]
    if not files:
        sys.stderr.write(f"No files in {folder}\n")
        sys.exit(1)

    out = {"date": date_str, "files": sorted(files)}
    json.dump(out, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list-public-files.py
```

---

### 2) `bin/dataset-enrich.sh` (key excerpts)

```bash
#!/usr/bin/env bash
set -euo pipefail

HF_REPO="datasets/axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"

# Deterministic shard assignment
shard_for() {
  local slug=$1
  # stable hash across runs
  local hash
  hash=$(echo -n "$slug" | sha256sum | tr -d ' -')
  echo $(( 0x${hash:0:8} % TOTAL_SHARDS ))
}

process_file_cdn() {
  local file=$1
  local url="https://huggingface.co/${HF_REPO}/resolve/main/${file}"
  # download via CDN (no auth) and project to {prompt,response}
  curl -fsSL "$url" -o "/tmp/$(basename "$file")"
  # ... existing projection / normalization logic ...
}

process_file_streaming() {
  local file=$1
  # existing HF dataset streaming path (fallback)
  python -c "
from datasets import load_dataset
ds = load_dataset('${HF_REPO}', name='default', split='train', streaming=True)
# ... filter by file and project ...
"
}

main() {
  if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
    mapfile -t FILES < <(jq -r '.files[]' "$FILE_LIST")
  else
    # fallback: list via HF API (legacy)
    mapfile -t FILES < <(huggingface-cli repo ls --repo-type dataset "$HF_REPO" --path "batches/public-raw/$(date +%F)/" || true)
  fi

  for f in "${FILES[@]}"; do
    slug=$(basename "$f" | sed 's/\.[^.]*$//')
    if [[ $(shard_for "$slug") -ne $SHARD_ID ]]; then
      continue
    fi

    if [[ -n "$FILE_LIST" ]]; then
      process_file_cdn "$f"
    else
      process_file_streaming "$f"
    fi
  done
}

main "$@"
```

Ensure executable:

```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3) `.github/workflows/ingest.yml` (key excerpts)

```yaml
name: ingest

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  list-files:
    runs-on: ubuntu-latest
    outputs:
      file_list: ${{ steps.set.outputs.file_list }}
      has_files: ${{ steps.set.outputs.has_files }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - run: python bin/list-public-files.py > public-files.json || true
      - id: set
        run: |
          if [[ -f public-files.json && $(jq '.files | length' public-files.json) -gt 0 ]]; then
            echo "has_files=true" >> "$GITHUB_OUTPUT"
            echo "file_list=$(realpath public-files.json)" >> "$GITHUB_OUTPUT"
          else
            echo "has_files=false" >> "$GITHUB_OUTPUT"
          fi
      - uses: actions/upload-artifact@v4
        if: steps.set.outputs.has_files == 'true'
        with:
          name: public-files
          path: public-files.json

  ingest-shards:
    needs: list-files
    if: needs.list-files.outputs.has_files == 'true'
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: public-files
          path: .
      - run: chmod +x bin/dataset-enrich.sh
      - env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          SHARD_ID: ${{ matrix.shard_id }}
          TOTAL_SHARDS: 16
          FILE_LIST: public-files.json
        run: ./bin/dataset-enrich.sh
```

---

## Verification (local)

```bash
# 1) Generate file list
python bin/list-public-files.py > public-files.json

# 2) Run a single shard locally (simulate)
HF_TOKEN=... SHARD_ID=0 TOTAL_SHARDS=16 FILE_LIST=public-files.json ./bin/dataset-enrich.sh
```

Expected: shard 0 processes only its deterministic slice using CDN URLs; no HF API calls during download.
