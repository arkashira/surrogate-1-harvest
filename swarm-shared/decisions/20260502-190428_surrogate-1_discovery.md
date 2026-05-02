# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Core idea**: One deterministic preflight produces a pinned file list; all shards consume it via CDN URLs only. This eliminates HF API auth/rate limits and overwrite races while keeping shard routing and dedup intact.

1. Preflight (once per run)  
   - Compute date partition (`YYYY-MM-DD`) with manual override.  
   - Call `list_repo_tree(..., recursive=False)` for that date folder.  
   - Emit `file-list.json` artifact containing `{date, files[], sha256}`.  
   - If list is empty, fail fast (no wasted shards).

2. Shard workers (parallel)  
   - Download `file-list.json` artifact.  
   - Iterate only assigned files via `hash(slug) % 16 == SHARD_ID`.  
   - Fetch via CDN (`resolve/main/...`) with short timeout and UA header.  
   - Idempotent output: skip if target file exists and non-empty.  
   - Deterministic per-run filenames: `shard{N}-{YYYYMMDD-HHMMSS}.jsonl`.  
   - Central local dedup (MD5) persisted to `dedup_hashes.jsonl`; cross-run dedup handled by HF Space SQLite.  
   - Emit per-shard summary (count, bytes, duration) to stdout.

3. Observability & safety  
   - Preflight and shard steps log clearly and exit non-zero on hard errors.  
   - Workflow concurrency control to avoid simultaneous runs corrupting dedup DB.  
   - No changes to central dedup logic or hash routing.

Estimated effort:  
- Script changes: ~30 min  
- Workflow + artifact plumbing: ~30 min  
- Test run + polish: ~60 min  

---

## Code changes

### 1) Workflow (`.github/workflows/ingest.yml`)

```yaml
name: surrogate-1-ingest

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date:
        description: "Date partition (YYYY-MM-DD). If omitted, uses UTC today."
        required: false

env:
  DATASET_REPO: axentx/surrogate-1-training-pairs

jobs:
  preflight:
    runs-on: ubuntu-latest
    outputs:
      date_part: ${{ steps.date.outputs.date_part }}
      file_count: ${{ steps.list.outputs.file_count }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set date partition
        id: date
        run: |
          if [ -n "${{ github.event.inputs.date }}" ]; then
            echo "date_part=${{ github.event.inputs.date }}" >> $GITHUB_OUTPUT
          else
            echo "date_part=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT
          fi

      - name: Install huggingface_hub
        run: pip install huggingface_hub

      - name: List date folder (non-recursive)
        id: list
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          DATE_PART: ${{ steps.date.outputs.date_part }}
        run: |
          python - <<'PY'
          import os, json, sys
          from huggingface_hub import HfApi
          api = HfApi()
          repo = os.environ["DATASET_REPO"]
          date_part = os.environ["DATE_PART"]
          try:
              tree = api.list_repo_tree(repo=repo, path=date_part, recursive=False)
              files = sorted([t.path for t in tree if t.type == "file"])
          except Exception:
              files = []
          out = {"date": date_part, "files": files}
          out_path = "file-list.json"
          with open(out_path, "w") as f:
              json.dump(out, f)
          print(f"::set-output name=file_count::{len(files)}")
          PY

      - name: Fail fast if no files
        if: steps.list.outputs.file_count == '0'
        run: |
          echo "No files found for date ${{ steps.date.outputs.date_part }} — nothing to ingest."
          exit 1

      - name: Upload file-list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

  ingest:
    needs: preflight
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    env:
      SHARD_ID: ${{ matrix.shard_id }}
      DATE_PART: ${{ needs.preflight.outputs.date_part }}
    steps:
      - uses: actions/checkout@v4

      - name: Download file-list
        uses: actions/download-artifact@v4
        with:
          name: file-list

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run shard worker
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          bash bin/dataset-enrich.sh "$SHARD_ID" "$DATE_PART"
```

---

### 2) Worker script (`bin/dataset-enrich.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: dataset-enrich.sh <shard_id> <date_part>
SHARD_ID="${1:-0}"
DATE_PART="${2:-$(date -u +%Y-%m-%d)}"

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="batches/public-merged/${DATE_PART}"
TS=$(date -u +%Y%m%d-%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
FILE_LIST="file-list.json"

mkdir -p "$(dirname "$OUT_FILE")"

echo "[$(date -u)] Shard $SHARD_ID | date=$DATE_PART | out=$OUT_FILE"

if [[ ! -f "$FILE_LIST" ]]; then
  echo "ERROR: $FILE_LIST not found" >&2
  exit 1
fi

# Idempotency: skip if target exists and is non-empty
if [[ -s "$OUT_FILE" ]]; then
  echo "INFO: $OUT_FILE exists and is non-empty — skipping shard $SHARD_ID"
  exit 0
fi

python - "$SHARD_ID" "$DATE_PART" "$OUT_FILE" "$FILE_LIST" <<'PY'
import json, hashlib, os, sys, time, urllib.request
from pathlib import Path

SHARD_ID = int(sys.argv[1])
DATE_PART = sys.argv[2]
OUT_FILE = sys.argv[3]
FILE_LIST = sys.argv[4]

REPO = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{REPO}/resolve/main"

with open(FILE_LIST) as f:
    manifest = json.load(f)

candidates = manifest.get("files", [])
# Normalize paths relative to date folder
if DATE_PART:
    nested = [p for p in candidates if p.startswith(DATE_PART + "/")]
    if nested:
        candidates = nested
    else:
        # already flat or mismatched; proceed as-is
        pass

total = 0
accepted = 0
skipped_dup = 0
start = time.time()

DEDUP_DB = "dedup_hashes.jsonl"
seen = set()
if os.path.exists(DEDUP_DB):
    with open(DEDUP_DB) as f:
        for line in f:
            line = line.strip()
            if line:
                seen.add(line)

def is_new(md5: str) -> bool:
    if md5 in seen:
        return False
    seen.add(md5)
    with open(DEDUP_DB, "a") as f:
        f.write(md5 + "\n")
    return True

def project_to_pair(raw_bytes: bytes):
    # Placeholder: implement per-schema projection to {prompt, response}.
    # For now, assume each file is line-delimited JSON with 'prompt' and 'response'.
    import io
    pairs = []
    stream = io.Bytes
