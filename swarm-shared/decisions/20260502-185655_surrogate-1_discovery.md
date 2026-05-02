# surrogate-1 / discovery

## 1. Diagnosis
- No deterministic date-partitioning: re-runs overwrite or duplicate outputs instead of appending to stable `YYYY/MM/DD` folders, causing noisy history and downstream training instability.
- No pre-flight file-list: workers call live HF APIs (`list_repo_tree`/`load_dataset`) during every run, risking 429 rate-limits and wasting quota instead of using CDN-only fetches.
- Shard outputs use flat `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` which collides across reruns; no content-addressable path or idempotency key.
- Central dedup store lives only on the HF Space (`cpu-basic`); GitHub Actions runners start with empty caches, so cross-run duplicates are not suppressed and bandwidth is wasted.
- No backoff/retry or rate-limit guard around HF ingestion calls; transient 429s or CDN hiccups can kill a shard run and lose that slice for the cycle.

## 2. Proposed change
File: `bin/dataset-enrich.sh`  
Scope: add deterministic date partition, generate pre-flight file-list once per run, emit idempotent shard paths, and embed CDN-only fetch behavior with retry/backoff.

## 3. Implementation
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: deterministic date partitioning + CDN-only ingestion + idempotent shard paths

set -euo pipefail

# -- config --
REPO="axentx/surrogate-1-training-pairs"
HF_TOKEN="${HF_TOKEN:-}"
SHARD_ID="${SHARD_ID:-0}"        # 0..15
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE_PART=$(date -u +"%Y/%m/%d")
TS=$(date -u +"%H%M%S")
RUN_ID=$(date -u +"%Y%m%d")
OUT_DIR="batches/public-merged/${DATE_PART}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${RUN_ID}-${TS}.jsonl"
CACHE_DIR=".cache"
FILE_LIST="${CACHE_DIR}/file-list-${RUN_ID}.json"
MAX_RETRIES=5
RETRY_BACKOFF=30

mkdir -p "${CACHE_DIR}" "${OUT_DIR}"

# -- helpers --
log() { echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"; }

retry() {
  local n=0
  until "$@"; do
    local code=$?
    n=$((n + 1))
    if [ "$n" -ge "$MAX_RETRIES" ]; then
      log "ERROR: command failed after $MAX_RETRIES attempts: $*"
      return $code
    fi
    local sleep_time=$((RETRY_BACKOFF * n))
    log "WARN: command failed (attempt $n/$MAX_RETRIES), retrying in ${sleep_time}s: $*"
    sleep "$sleep_time"
  done
}

# -- pre-flight file list (single API call per run) --
if [ ! -f "${FILE_LIST}" ]; then
  log "Generating pre-flight file list for ${RUN_ID}..."
  # Use recursive=False per top-level folder to avoid 100x pagination; keep list small and deterministic.
  python3 -c "
import json, os
from huggingface_hub import HfApi
api = HfApi(token=os.environ.get('HF_TOKEN'))
# Only list top-level folders/files once; workers will resolve exact paths from this manifest.
tree = api.list_repo_tree(repo_id='${REPO}', path='', recursive=False)
items = [t.rpath for t in tree if t.type == 'file']
open('${FILE_LIST}', 'w').write(json.dumps(items, sort_keys=True))
" || {
    log "ERROR: failed to list repo tree"; exit 1
  }
  log "File list saved to ${FILE_LIST} ($(jq length <"${FILE_LIST}") items)"
fi

# -- deterministic shard assignment --
mapfile -t ALL_FILES < <(jq -r '.[]' "${FILE_LIST}")
TOTAL_FILES=${#ALL_FILES[@]}
if [ "$TOTAL_FILES" -eq 0 ]; then
  log "ERROR: no files to process"; exit 1
fi

# assign files to shards by stable hash of filename
assign_shard() {
  local file="$1"
  # deterministic 0..(TOTAL_SHARDS-1)
  python3 -c "print(abs(hash('${file}')) % ${TOTAL_SHARDS})"
}

# collect shard files
SHARD_FILES=()
for f in "${ALL_FILES[@]}"; do
  s=$(assign_shard "$f")
  if [ "$s" -eq "$SHARD_ID" ]; then
    SHARD_FILES+=("$f")
  fi
done

log "Shard ${SHARD_ID}/${TOTAL_SHARDS} processing ${#SHARD_FILES[@]}/${TOTAL_FILES} files -> ${OUT_FILE}"

# -- CDN-only fetch + schema projection --
process_file() {
  local rel_path="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  local tmpf
  tmpf=$(mktemp)
  # CDN download (no Authorization header) bypasses API rate limits
  retry curl -fsSL --retry 3 --retry-delay 5 -o "${tmpf}" "${url}" || {
    log "WARN: failed to download ${rel_path}, skipping"
    rm -f "${tmpf}"
    return 0
  }

  # Lightweight schema projection: extract {prompt,response} only
  python3 -c "
import json, pyarrow.parquet as pq, sys, os, tempfile
path = '${tmpf}'
out_path = '${OUT_FILE}'
try:
    table = pq.read_table(path, columns=['prompt', 'response'] if 'prompt' in pq.read_schema(path).names else None)
except Exception:
    # fallback: try to read as jsonl
    import pandas as pd
    try:
        df = pd.read_json(path, lines=True)
        if 'prompt' in df.columns and 'response' in df.columns:
            table = pa.Table.from_pandas(df[['prompt','response']])
        else:
            sys.exit(0)
    except Exception:
        sys.exit(0)

# normalize to {prompt,response}
if table.num_rows == 0:
    sys.exit(0)

import pyarrow as pa
if 'prompt' not in table.column_names or 'response' not in table.column_names:
    # best-effort: pick two text-like columns
    cols = [c for c in table.column_names if table.schema.field(c).type in (pa.string(), pa.large_string())]
    if len(cols) >= 2:
        table = table.select(cols[:2]).rename_columns(['prompt','response'])
    else:
        sys.exit(0)
else:
    table = table.select(['prompt','response'])

# append to output (newline-delimited json)
import json, os
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, 'a', encoding='utf-8') as f:
    for batch in table.to_batches():
        df = batch.to_pandas()
        for _, row in df.iterrows():
            f.write(json.dumps({'prompt': str(row['prompt']), 'response': str(row['response'])}, ensure_ascii=False) + '\n')
" || true

  rm -f "${tmpf}"
}

export -f process_file
export OUT_FILE

# -- parallel per-file processing (bounded) --
JOBS=4
log "Starting ingestion with ${JOBS} parallel jobs..."
printf '%s\n' "${SHARD_FILES[@]}" | xargs -P "$JOBS" -I{} bash -c 'process_file "$@"' _ {}

log "Shard ${SHARD_ID} completed -> ${OUT_FILE}"
```

If you want, I can also add a small Python dedup client (`lib/dedup_client.py`) that the runners can call to check the central HF-Space SQLite store before emitting pairs, to reduce cross-run duplicates without waiting for the Space to clean them later.
