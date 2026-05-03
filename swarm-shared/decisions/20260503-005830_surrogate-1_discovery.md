# surrogate-1 / discovery

## Final Synthesized Implementation  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*

---

## Chosen Fix (Highest-Value, <2h)

**Eliminate HF API rate-limit failures during ingestion** by:

1. **Listing files once per date folder** (single `tree` API call) from the Mac orchestrator.  
2. **Embedding that file list** into the training/shard runner.  
3. **Fetching raw files via CDN (`resolve/main/...`)** during ingestion — zero auth/rate-limit usage for data download.  
4. **Keeping schema projection in Python** (pyarrow), but only projecting `{prompt, response}` to avoid `CastError` from heterogeneous schemas.  
5. **Pushing results to HF dataset repo** via git (deterministic filenames prevent collisions).

This directly fixes the 429 pattern, prevents wasted Actions minutes, and requires no Python refactor.

---

## Concrete Implementation Plan

### 1. Mac Helper: `bin/list-date-folder.sh`  
Run once per date folder; outputs deterministic file list.

```bash
#!/usr/bin/env bash
# bin/list-date-folder.sh
# Usage: ./list-date-folder.sh 2026-05-03 > file-list-2026-05-03.json
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
HF_TOKEN="${HF_TOKEN:-}"

URL="https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE_FOLDER}&recursive=false"

if [[ -n "$HF_TOKEN" ]]; then
  curl -sL -H "Authorization: Bearer ${HF_TOKEN}" "$URL"
else
  curl -sL "$URL"
fi
```

---

### 2. Worker: `bin/dataset-enrich.sh`  
Runs in GitHub Actions (16-shard matrix). Uses pre-listed file list + CDN bypass.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Runs in GitHub Actions (16-shard matrix)
set -euo pipefail

# Config
REPO="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
OUT_DIR="./output"
mkdir -p "$OUT_DIR"

# 1. Accept pre-listed file list (embedded or downloaded)
FILE_LIST="${FILE_LIST:-/tmp/file-list-${DATE_FOLDER}.json}"

# If file list not provided, fetch once per shard start (fallback)
if [[ ! -f "$FILE_LIST" ]]; then
  echo "[$SHARD_ID] Fetching file list for $DATE_FOLDER..."
  URL="https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE_FOLDER}&recursive=false"
  if [[ -n "$HF_TOKEN" ]]; then
    curl -sL -H "Authorization: Bearer ${HF_TOKEN}" "$URL" -o "$FILE_LIST"
  else
    curl -sL "$URL" -o "$FILE_LIST"
  fi
fi

# Extract file paths
mapfile -t ALL_FILES < <(jq -r '.[] | select(.type=="file") | .path' "$FILE_LIST")
TOTAL_FILES=${#ALL_FILES[@]}

if [[ $TOTAL_FILES -eq 0 ]]; then
  echo "[$SHARD_ID] No files found for $DATE_FOLDER, exiting."
  exit 0
fi

# 2. Deterministic shard assignment: hash(path) mod TOTAL_SHARDS
process_file() {
  local filepath="$1"
  local slug_hash
  slug_hash=$(echo -n "$filepath" | md5sum | cut -c1-8)
  local shard_assign=$(( 0x${slug_hash} % TOTAL_SHARDS ))
  [[ $shard_assign -eq $SHARD_ID ]]
}

# 3. Filter to this shard's files
SHARD_FILES=()
for f in "${ALL_FILES[@]}"; do
  if process_file "$f"; then
    SHARD_FILES+=("$f")
  fi
done

echo "[$SHARD_ID] Processing ${#SHARD_FILES[@]}/${TOTAL_FILES} files (shard ${SHARD_ID}/${TOTAL_SHARDS})"

# 4. Process each file: CDN download + schema projection
TIMESTAMP=$(date +%H%M%S)
OUTPUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

process_and_project() {
  local filepath="$1"
  local cdn_url="https://huggingface.co/datasets/${REPO}/resolve/main/${filepath}"
  local tmp_file
  tmp_file=$(mktemp)

  if curl -sL -f "$cdn_url" -o "$tmp_file"; then
    python3 -c "
import sys, json, pyarrow.parquet as pq
try:
    table = pq.read_table('$tmp_file')
    prompt = table['prompt'].to_pylist() if 'prompt' in table.column_names else [None] * len(table)
    response = table['response'].to_pylist() if 'response' in table.column_names else [None] * len(table)
    for p, r in zip(prompt, response):
        if p is not None and r is not None:
            print(json.dumps({'prompt': p, 'response': r, 'source_file': '$filepath'}))
except Exception:
    pass
" >> "$OUTPUT_FILE" 2>/dev/null || true
    rm -f "$tmp_file"
  else
    echo "[$SHARD_ID] CDN download failed: $filepath" >&2
    rm -f "$tmp_file"
  fi
}

export -f process_and_project
export OUTPUT_FILE

# Parallel within shard
printf '%s\n' "${SHARD_FILES[@]}" | xargs -P 4 -I {} bash -c 'process_and_project "$@"' _ {}

# 5. Upload to HF dataset repo (git-based, deterministic filenames)
if [[ -s "$OUTPUT_FILE" ]]; then
  LINES=$(wc -l < "$OUTPUT_FILE")
  echo "[$SHARD_ID] Produced $LINES pairs -> shard${SHARD_ID}-${TIMESTAMP}.jsonl"

  if [[ -n "$HF_TOKEN" ]]; then
    git config --global user.email "runner@axentx.io"
    git config --global user.name "surrogate-1-runner"

    DATASET_DIR="/tmp/dataset-repo"
    rm -rf "$DATASET_DIR"
    git clone --depth 1 "https://${HF_TOKEN}@huggingface.co/datasets/${REPO}" "$DATASET_DIR"

    DEST_DIR="${DATASET_DIR}/batches/public-merged/${DATE_FOLDER}"
    mkdir -p "$DEST_DIR"
    cp "$OUTPUT_FILE" "${DEST_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

    cd "$DATASET_DIR"
    git add "batches/public-merged/${DATE_FOLDER}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"
    git commit -m "shard${SHARD_ID}: ${TIMESTAMP} - ${LINES} pairs"
    git push origin main

    echo "[$SHARD_ID] Commit pushed successfully"
  else
    echo "[$SHARD_ID] No HF_TOKEN, skipping upload (dry-run)"
  fi
else
  echo "[$SHARD_ID] No valid pairs produced"
fi
```

---

### 3. Lightning Training Integration (CDN-only, zero API calls)

In your training script, load the pre-listed file list and stream via CDN:

```python
import json
from pathlib import Path
import pyarrow.parquet as pq
import requests

def cdn_stream(file_list_path, repo="axentx/surrogate-1-training-pairs"):
    with open(file_list_path) as f:
        files = [item["path"] for item in json.load(f) if item["type"] == "file"]

    for path in files:
        url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
        tmp = Path("/tmp") / path.split("/")[-1]
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            tmp.write_bytes(resp.content)

            table = pq.read_table(tmp)
            if "prompt" in table.column_names and "response" in table.column_names:
                for row in table.to_p
