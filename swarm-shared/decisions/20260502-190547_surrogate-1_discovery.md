# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal:** deterministic ingestion, CDN-bypass, API-efficient, idempotent, and race-safe.

### Core decisions (resolve contradictions)
- Use **UTC date partition** `batches/public-merged/{YYYY-MM-DD}/` (both candidates agree).
- **Single pre-flight `list_repo_tree` call** per run (non-recursive) to produce `file-list.json`; embed it so workers never list during training (reduces API pressure and enables CDN-only fetches).
- **Shard assignment unchanged** (`slug-hash % 16`) but operate only on files from the pre-fetched list (deterministic and rate-limit safe).
- **Idempotent filenames** include `HHMMSS` in the worker output filename; skip upload if target exists (fast-fail/continue).
- **CDN-bypass download** via raw `https://huggingface.co/datasets/.../resolve/main/...` (no auth header) using the pre-computed list.
- **HF_TOKEN used only for upload**, not for listing during training.
- **Local test** with a mock list to verify paths and CDN URLs resolve.

---

### 1) New script: `bin/build-file-list.sh` (pre-flight generator)

```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: RUN_DATE=YYYY-MM-DD ./bin/build-file-list.sh
# Produces: file-list.json  (JSON object with date + files array)

: "${HF_TOKEN:?}"
: "${RUN_DATE:=$(date -u +%Y-%m-%d)}"

REPO="datasets/axentx/surrogate-1-training-pairs"

python -c "
import json, os, datetime, sys
from huggingface_hub import HfApi
api = HfApi(token=os.getenv('HF_TOKEN'))
repo = 'datasets/axentx/surrogate-1-training-pairs'
today = os.getenv('RUN_DATE', datetime.datetime.utcnow().strftime('%Y-%m-%d'))
files = [f.rfilename for f in api.list_repo_tree(repo, path=today, recursive=False)]
out = {'date': today, 'files': sorted(files)}
with open('file-list.json', 'w') as f:
    json.dump(out, f, indent=2)
print(json.dumps(out))
"
```

---

### 2) Updated workflow: `.github/workflows/ingest.yml`

```yaml
name: ingest
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

jobs:
  file-list:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.vars.outputs.date }}
      file_list: ${{ steps.list.outputs.files }}
    steps:
      - uses: actions/checkout@v4
      - name: Install deps
        run: pip install huggingface_hub
      - name: Generate file list (single API call)
        id: list
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          RUN_DATE: ""  # let script default to UTC today
        run: |
          bash bin/build-file-list.sh
          echo "files=$(jq -c .files file-list.json)" >> $GITHUB_OUTPUT
          echo "date=$(jq -r .date file-list.json)" >> $GITHUB_OUTPUT
      - name: Upload file-list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

  ingest-shard:
    needs: file-list
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - name: Download file-list
        uses: actions/download-artifact@v4
        with:
          name: file-list
      - name: Install deps
        run: pip install -r requirements.txt
      - name: Run shard worker
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          FILE_LIST: file-list.json
          RUN_DATE: ${{ needs.file-list.outputs.date }}
        run: |
          bash bin/dataset-enrich.sh
```

---

### 3) Updated worker: `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${SHARD_ID:?}"
: "${HF_TOKEN:?}"
: "${RUN_DATE:?}"
: "${FILE_LIST:?}"

REPO="datasets/axentx/surrogate-1-training-pairs"
OUT_DIR="output"
mkdir -p "$OUT_DIR"

# Parse file list (JSON array of filenames)
if [[ -f "$FILE_LIST" ]]; then
  mapfile -t FILES < <(jq -r '.files[]' "$FILE_LIST")
else
  echo "No file list provided at $FILE_LIST" >&2
  exit 1
fi

# Deterministic output path (idempotent)
TS=$(date -u +"%H%M%S")
OUT_PATH="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

# Helper: CDN URL (no auth, bypass API rate limits)
cdn_url() {
  local f="$1"
  echo "https://huggingface.co/datasets/${REPO}/resolve/main/${f}"
}

# Process only files assigned to this shard
processed=0
for f in "${FILES[@]}"; do
  # Deterministic shard assignment by slug-hash
  slug=$(basename "$f" | sed 's/\.[^.]*$//')
  hash=$(echo -n "$slug" | md5sum | cut -c1-8)
  shard=$(( 0x$hash % 16 ))
  if [[ $shard -ne $SHARD_ID ]]; then
    continue
  fi

  echo "Processing shard=$SHARD_ID file=$f"
  url=$(cdn_url "$f")
  tmp=$(mktemp)
  # Download via CDN (no Authorization header)
  curl -fsSL "$url" -o "$tmp"

  # Parse and project to {prompt,response} only
  python -c "
import sys, json, pyarrow.parquet as pq, os
tmp = sys.argv[1]
out = sys.argv[2]
try:
    table = pq.read_table(tmp)
    df = table.to_pandas()
    # Keep only prompt/response columns if present; adapt per schema
    if 'prompt' in df.columns and 'response' in df.columns:
        selected = df[['prompt','response']]
    else:
        # fallback: use first two text-like columns
        cols = [c for c in df.columns if df[c].dtype == 'object']
        selected = df[cols[:2]].rename(columns={cols[0]:'prompt', cols[1]:'response'})
    selected.dropna(subset=['prompt','response'], inplace=True)
    with open(out, 'a') as f:
        for _, row in selected.iterrows():
            f.write(json.dumps({'prompt': row['prompt'], 'response': row['response']}, ensure_ascii=False) + '\n')
except Exception as e:
    print('Parse error:', e, file=sys.stderr)
  " "$tmp" "$OUT_PATH"

  rm -f "$tmp"
  processed=$((processed + 1))
done

echo "Shard $SHARD_ID processed $processed files -> $OUT_PATH"

# Upload to HF dataset repo (date-partitioned, idempotent)
if [[ -s "$OUT_PATH" ]]; then
  DEST_PATH="batches/public-merged/${RUN_DATE}/shard${SHARD_ID}-${TS}.jsonl"
  # Skip upload if file already exists (idempotency)
  if huggingface-cli repo ls --repo-type dataset "$REPO" "$DEST_PATH" --token "$HF_TOKEN" >/dev/null 2>&1; then
    echo "File $DEST_PATH already exists; skipping upload (idempotent)."
  else
    huggingface-cli upload --repo-type dataset "$REPO" "$OUT_PATH" "$DEST_PATH" --token "$HF_TOKEN"
    echo "Uploaded to $DEST_PATH"
  fi
else
  echo "No output to upload for shard $SHARD_ID"
fi
```

---

### 4) Local test (smoke)

```bash
# Generate a mock file list for a test date
RUN_DATE=
