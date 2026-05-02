# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Eliminate HF API rate-limit risk during ingestion by generating a deterministic file-list snapshot once, then using CDN-only downloads during parallel shard processing. This applies the key insight from the training pipeline patterns (HF CDN bypass + pre-list once) to the surrogate-1-runner ingestion workers.

### 1) Snapshot script — `bin/snapshot.sh` (20 min)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Generate deterministic snapshot of dataset file list for a date folder
# Usage: ./bin/snapshot.sh <date> [output-json]
# Example: ./bin/snapshot.sh 2026-05-02 snapshot-2026-05-02.json

DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-snapshot-${DATE}.json}"
REPO="axentx/surrogate-1-training-pairs"

echo "[$(date -Iseconds)] Generating snapshot for ${DATE} -> ${OUT}"

# Single API call: list top-level folder only (no recursion, no pagination pressure)
# Uses gh CLI (authenticated) or falls back to curl with HF token
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  FILES=$(gh api "repos/${REPO}/contents/batches/public-merged/${DATE}" --paginate --jq '.[].name' 2>/dev/null || true)
else
  # Fallback: use HF API with token (rate-limited)
  HF_TOKEN="${HF_TOKEN:-}"
  if [ -z "$HF_TOKEN" ]; then
    echo "ERROR: No gh CLI auth and no HF_TOKEN set" >&2
    exit 1
  fi
  FILES=$(curl -s -H "Authorization: Bearer ${HF_TOKEN}" \
    "https://huggingface.co/api/datasets/${REPO}/tree/batches/public-merged/${DATE}?recursive=false" \
    | jq -r '.[].path' 2>/dev/null || true)
fi

if [ -z "$FILES" ]; then
  echo "WARNING: No files found for ${DATE}, creating empty snapshot"
  FILES="[]"
else
  # Convert newline list to JSON array
  FILES=$(echo "$FILES" | jq -R -s -c 'split("\n") | map(select(. != ""))')
fi

# Deterministic ordering for shard assignment
echo "$FILES" | jq -c 'sort' > "${OUT}.tmp"
mv "${OUT}.tmp" "${OUT}"

echo "[$(date -Iseconds)] Snapshot written: ${OUT} ($(jq length "${OUT}") files)"
```

### 2) Updated worker — `bin/dataset-enrich.sh` (40 min)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Surrogate-1 ingestion worker (shard processor)
# Usage: ./bin/dataset-enrich.sh <shard_id> <total_shards> [snapshot.json]
#
# Environment:
#   HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
#   SHARD_ID          - 0..15 (matrix index)
#   TOTAL_SHARDS      - 16 (matrix size)

SHARD_ID="${1:-$SHARD_ID}"
TOTAL_SHARDS="${2:-$TOTAL_SHARDS}"
SNAPSHOT="${3:-snapshot-$(date +%Y-%m-%d).json}"

if [ -z "${SHARD_ID:-}" ] || [ -z "${TOTAL_SHARDS:-}" ]; then
  echo "ERROR: SHARD_ID and TOTAL_SHARDS required" >&2
  exit 1
fi

DATE=$(date +%Y-%m-%d)
TS=$(date +%H%M%S)
OUTPUT_REPO="axentx/surrogate-1-training-pairs"
OUTPUT_PATH="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"

echo "[$(date -Iseconds)] Worker shard=${SHARD_ID}/${TOTAL_SHARDS} snapshot=${SNAPSHOT}"

# Load deterministic file list from snapshot (single API call done by snapshot.sh)
if [ ! -f "$SNAPSHOT" ]; then
  echo "ERROR: Snapshot ${SNAPSHOT} not found. Run bin/snapshot.sh first." >&2
  exit 1
fi

# Assign files to shards by deterministic hash
mapfile -t ALL_FILES < <(jq -r '.[]' "$SNAPSHOT")
SHARD_FILES=()
for f in "${ALL_FILES[@]}"; do
  # Deterministic shard assignment: hash slug mod TOTAL_SHARDS
  HASH=$(echo -n "$f" | md5sum | cut -c1-8)
  HASH_DEC=$((16#$HASH))
  ASSIGNED=$((HASH_DEC % TOTAL_SHARDS))
  if [ "$ASSIGNED" -eq "$SHARD_ID" ]; then
    SHARD_FILES+=("$f")
  fi
done

echo "[$(date -Iseconds)] Assigned ${#SHARD_FILES[@]} files to shard ${SHARD_ID}"

# Process assigned files using CDN-only downloads (zero HF API during ingest)
TMP_OUT=$(mktemp /tmp/shard-${SHARD_ID}-XXXX.jsonl)
trap 'rm -f "$TMP_OUT"' EXIT

for rel_path in "${SHARD_FILES[@]}"; do
  # CDN download: no Authorization header, bypasses API rate limit
  URL="https://huggingface.co/datasets/${OUTPUT_REPO}/resolve/main/${rel_path}"
  
  echo "[$(date -Iseconds)] Downloading ${rel_path} via CDN..."
  
  # Download to temp file, handle errors gracefully
  if ! curl -s -f -L "$URL" -o "/tmp/dl-$$.parquet"; then
    echo "WARNING: Failed to download ${rel_path}, skipping" >&2
    continue
  fi
  
  # Process parquet -> {prompt, response} projection using Python helper
  python3 -c "
import pyarrow.parquet as pq
import json
import sys
try:
    table = pq.read_table(sys.argv[1], columns=['prompt', 'response'])
    for i in range(table.num_rows):
        row = table.slice(i, 1).to_pydict()
        prompt = row.get('prompt', [''])[0]
        response = row.get('response', [''])[0]
        if prompt and response:
            print(json.dumps({'prompt': str(prompt), 'response': str(response)}))
except Exception as e:
    # Fallback: try generic schema
    try:
        table = pq.read_table(sys.argv[1])
        cols = table.column_names
        # Heuristic: first text col as prompt, second as response
        text_cols = [c for c in cols if table.schema.field(c).type in ('string', 'large_string')]
        if len(text_cols) >= 2:
            for i in range(min(table.num_rows, 100)):  # limit for safety
                row = table.slice(i, 1).to_pydict()
                prompt = row.get(text_cols[0], [''])[0]
                response = row.get(text_cols[1], [''])[0]
                if prompt and response:
                    print(json.dumps({'prompt': str(prompt), 'response': str(response)}))
    except Exception as e2:
        print(f'ERROR processing {sys.argv[1]}: {e2}', file=sys.stderr)
" "/tmp/dl-$$.parquet" >> "$TMP_OUT" 2>/dev/null || true
  
  rm -f "/tmp/dl-$$.parquet"
done

# Dedup via central md5 store (existing lib/dedup.py)
if [ -s "$TMP_OUT" ]; then
  echo "[$(date -Iseconds)] Running dedup..."
  python3 lib/dedup.py "$TMP_OUT" > "${TMP_OUT}.dedup"
  mv "${TMP_OUT}.dedup" "$TMP_OUT"
  
  LINES=$(wc -l < "$TMP_OUT")
  echo "[$(date -Iseconds)] Uploading ${LINES} lines to ${OUTPUT_PATH}"
  
  # Upload to HF dataset (single commit per shard)
  if [ -n "${HF_TOKEN:-}" ]; then
    huggingface-cli upload --repo-type dataset "$OUTPUT_REPO" "$TMP_OUT" "$OUTPUT_PATH" --token "$HF_TOKEN"
  else
    echo "WARNING: HF_TOKEN not set, skipping upload (dry-run)"
    cp "$TMP_OUT" "./dryrun-${OUTPUT_PATH//\//-}"
  fi
else
  echo "[$(date -Iseconds)] No data to upload for shard ${SHARD
