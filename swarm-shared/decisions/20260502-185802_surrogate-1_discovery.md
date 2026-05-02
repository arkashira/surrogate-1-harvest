# surrogate-1 / discovery

## Highest-value incremental improvement (≤2h)
Ship deterministic date-partitioned ingestion with CDN-bypass and a pre-flight file list to eliminate redundant API calls, overwrite races, and rate-limit pressure.

### Why this first
- Fixes noisy history and training instability (deterministic `YYYY/MM/DD` outputs).
- Cuts HF API traffic during ingestion (single Mac-side `list_repo_tree` → JSON; workers use CDN URLs).
- Enables safe re-runs and shard isolation without collisions.
- Fits existing 16-shard matrix; minimal code change, high leverage.

---

## Implementation plan

1. Add `YYYY/MM/DD` partition logic to output path
   - Use UTC date of run: `batches/public-merged/2026/05/02/shard03-142311.jsonl`
   - Include shard id + HHMMSS to keep filenames unique and monotonic.

2. Pre-flight file list (Mac orchestrator)
   - One-time (or cron-start) call: `list_repo_tree(recursive=False)` per date folder on upstream dataset repo.
   - Save to `file-list-YYYYMMDD.json` (array of `{"path":..., "sha256":...}`).
   - Commit or upload as workflow artifact; workers consume it instead of listing repos.

3. CDN-only downloads in workers
   - Replace `hf_hub_download`/`load_dataset` calls with direct CDN fetch:
     `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>`
   - No Authorization header; avoids API rate limits during streaming.

4. Deterministic shard assignment
   - Keep existing `slug-hash % 16 == SHARD_ID` logic.
   - Map each file from the pre-flight list to a shard; workers process only assigned paths.

5. Idempotent uploads
   - Target path includes date+shard+timestamp; never overwrite previous runs.
   - Workers skip upload if target file already exists (optional guard via `hf_hub_file_exists`).

6. Workflow changes
   - Add optional `file_list` input (artifact or repo path) to `ingest.yml`.
   - If absent, fall back to legacy live list (backwards compatible).
   - Pass `RUN_DATE` and `SHARD_ID` via matrix; compose output path in `dataset-enrich.sh`.

7. Validation & rollout
   - Dry-run one shard locally with mocked file list.
   - Check output path format and CDN fetch success.
   - Trigger full 16-shard run; confirm no 429s and date folders appear.

---

## Code snippets

### bin/dataset-enrich.sh (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Inputs
: "${HF_TOKEN:?required}"
: "${SHARD_ID:?required (0-15)}"
: "${RUN_DATE:?YYYY-MM-DD, e.g. 2026-05-02}"
: "${FILE_LIST:?path to file-list-YYYYMMDD.json (or 'live')}"

export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"

REPO="axentx/surrogate-1-training-pairs"
DATE_PART=$(echo "$RUN_DATE" | tr -d '-')
YEAR=$(echo "$RUN_DATE" | cut -d- -f1)
MONTH=$(echo "$RUN_DATE" | cut -d- -f2)
DAY=$(echo "$RUN_DATE" | cut -d- -f3)
TS=$(date -u +"%H%M%S")
OUT_NAME="shard$(printf '%02d' "$SHARD_ID")-${TS}.jsonl"
OUT_PATH="batches/public-merged/${YEAR}/${MONTH}/${DAY}/${OUT_NAME}"

log() { echo "[$(date -u -Iseconds)] $*"; }

# Resolve files to process
resolve_files() {
  if [ "$FILE_LIST" = "live" ]; then
    log "WARN: using live HF API (slow, rate-limited)"
    python -c "
import json, os
from huggingface_hub import list_repo_tree
files = [f.rfilename for f in list_repo_tree(repo_id='$REPO', recursive=False)]
print(json.dumps(files))
"
  else
    # Expect file-list-YYYYMMDD.json as array of objects with .path
    python -c "import json,sys; d=json.load(open('$FILE_LIST')); print(json.dumps([x['path'] for x in d]))"
  fi
}

# Deterministic shard assignment by filename slug
assign_shard() {
  local file="$1"
  # stable hash -> 0..15
  local hash
  hash=$(echo -n "$file" | sha256sum | tr -d ' -' | xxd -r -p | od -An -t u8 | tr -d ' ')
  echo $(( hash % 16 ))
}

# CDN download (no auth)
cdn_download() {
  local src_path="$1"
  local out_file="$2"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${src_path}"
  curl -fsSL --retry 3 --retry-delay 5 -o "$out_file" "$url"
}

log "Starting shard ${SHARD_ID} for ${RUN_DATE} -> ${OUT_PATH}"
mkdir -p "$(dirname "$OUT_PATH")"

# Stream process
tmp_out=$(mktemp)
trap 'rm -f "$tmp_out"' EXIT

resolve_files | python -c "import sys,json; print('\n'.join(json.load(sys.stdin)))" | while IFS= read -r fpath; do
  target_shard=$(assign_shard "$fpath")
  if [ "$target_shard" -ne "$SHARD_ID" ]; then
    continue
  fi

  log "Processing ${fpath} (shard ${target_shard})"
  tmp_dl=$(mktemp)
  if cdn_download "$fpath" "$tmp_dl"; then
    # Project to {prompt,response} here per surrogate-1 schema rules
    python -c "
import json, pyarrow.parquet as pq, sys, os
try:
    table = pq.read_table('$tmp_dl')
    df = table.to_pandas()
    # Minimal projection: keep only prompt/response or equivalent
    # Adapt field names per known schemas
    for pair in df.to_dict(orient='records'):
        prompt = pair.get('prompt') or pair.get('input') or pair.get('text') or ''
        response = pair.get('response') or pair.get('output') or ''
        if prompt or response:
            print(json.dumps({'prompt': str(prompt), 'response': str(response)}))
except Exception as e:
    print(json.dumps({'error': str(e), 'file': '$fpath'}), file=sys.stderr)
" >> "$tmp_out"
  else
    log "WARN: CDN download failed for ${fpath}"
  fi
  rm -f "$tmp_dl"
done

# Dedup (optional: rely on central store elsewhere) and upload
if [ -s "$tmp_out" ]; then
  # Sort/uniq by prompt+response hash to reduce local dupes within shard run
  python -c "
import sys, hashlib, json
seen = set()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    obj = json.loads(line)
    key = hashlib.md5((obj.get('prompt','')+obj.get('response','')).encode()).hexdigest()
    if key not in seen:
        seen.add(key)
        print(json.dumps(obj))
" < "$tmp_out" > "${tmp_out}.uniq"
  mv "${tmp_out}.uniq" "$tmp_out"

  log "Uploading ${OUT_PATH} ($(wc -l < "$tmp_out") lines)"
  huggingface-cli upload --repo-type dataset "$REPO" "$tmp_out" "$OUT_PATH" --token "$HF_TOKEN"
else
  log "No output for shard ${SHARD_ID}"
fi
```

### .github/workflows/ingest.yml (excerpt additions)
```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
