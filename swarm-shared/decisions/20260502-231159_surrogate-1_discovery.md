# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Goal**: Eliminate HF API 429s during training and make shard workers fully resilient by deterministic pre-flight file listing + CDN-only ingestion.

**Scope**:
- Add a one-time snapshot script (`bin/list-date-snapshot.sh`) that runs on the Mac orchestrator after rate-limit window clears, calls `list_repo_tree` once per date folder, and emits `snapshot/<date>/files.json`.
- Embed the snapshot into `bin/dataset-enrich.sh` so workers fetch via CDN URLs only (no `/api/` calls during streaming).
- Add retry/back-off for CDN downloads and a fallback to the snapshot when `list_repo_tree` fails.
- Keep HF_TOKEN usage only for repo write (upload) — never for read during ingestion.

**Why this fits <2h**:
- Single new script + small edits to existing worker.
- No infra changes; uses existing GitHub Actions matrix.
- Reuses patterns already proven: CDN bypass, deterministic shard, snapshot + embed.

---

## Concrete Changes

### 1) New: `bin/list-date-snapshot.sh`

```bash
#!/usr/bin/env bash
# list-date-snapshot.sh
# Usage: HF_TOKEN=... ./bin/list-date-snapshot.sh axentx surrogate-1-training-pairs 2026-05-02
# Produces: snapshot/<date>/files.json  (relative to repo root)

set -euo pipefail
REPO_OWNER="${1:-axentx}"
REPO_NAME="${2:-surrogate-1-training-pairs}"
DATE="${3:-$(date +%Y-%m-%d)}"
OUTDIR="snapshot/${DATE}"
OUTFILE="${OUTDIR}/files.json"

mkdir -p "${OUTDIR}"

# Use huggingface_hub from the runner environment (already in requirements.txt)
python3 -c "
import os, json, sys
from huggingface_hub import HfApi

api = HfApi(token=os.environ.get('HF_TOKEN'))
owner = os.environ.get('REPO_OWNER', '${REPO_OWNER}')
repo = os.environ.get('REPO_NAME', '${REPO_NAME}')
date_path = os.environ.get('DATE', '${DATE}')

# Non-recursive per-folder to avoid 100x pagination on big repos
tree = api.list_repo_tree(repo=repo, path=date_path, repo_type='dataset', recursive=False)
files = [f.rfilename for f in tree if f.type == 'file']

result = {
    'repo': {'owner': owner, 'name': repo},
    'date': date_path,
    'files': sorted(files),
    'snapshot_ts': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime())
}
sys.stdout.write(json.dumps(result, indent=2))
" > "${OUTFILE}.tmp"

mv "${OUTFILE}.tmp" "${OUTFILE}"
echo "Snapshot written: ${OUTFILE}"
```

Make executable:

```bash
chmod +x bin/list-date-snapshot.sh
```

---

### 2) Update: `bin/dataset-enrich.sh`

Key changes:
- Accept optional `SNAPSHOT_FILE` env var.
- If snapshot present, iterate files from snapshot and download via CDN URLs (no Authorization header).
- If snapshot absent, fall back to `load_dataset` with minimal API use (only for listing) and warn.
- Add CDN retry/back-off.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh  (updated)
# Existing behavior preserved; adds CDN-only mode via snapshot.

set -euo pipefail

HF_REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-snapshot/${DATE}/files.json}"
WORK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORK_DIR"

# Dedup store (existing)
DEDUP_DB="lib/dedup.py"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

cdn_download() {
  local url="$1"
  local out="$2"
  local max_retries=5
  local retry=0
  local delay=2

  while (( retry < max_retries )); do
    if curl -fsSL --retry 3 --retry-delay 1 -o "$out" "$url"; then
      return 0
    fi
    retry=$((retry + 1))
    log "CDN download failed (attempt $retry/$max_retries): $url"
    sleep "$delay"
    delay=$((delay * 2))
  done
  log "ERROR: Failed to download after $max_retries attempts: $url"
  return 1
}

process_file_cdn() {
  local file="$1"
  local tmpfile
  tmpfile="$(mktemp)"
  local url="https://huggingface.co/datasets/${HF_REPO}/resolve/main/${file}"

  if ! cdn_download "$url" "$tmpfile"; then
    rm -f "$tmpfile"
    return 1
  fi

  # Existing per-schema normalization logic goes here.
  # Example placeholder: project to {prompt,response} and emit JSONL lines.
  python3 -c "
import json, sys, hashlib, os, pyarrow.parquet as pq, pyarrow as pa
tmp = sys.argv[1]
try:
    table = pq.read_table(tmp)
    cols = table.column_names
    prompt_col = next((c for c in ('prompt','input','question') if c in cols), None)
    response_col = next((c for c in ('response','output','answer') if c in cols), None)
    if prompt_col is None or response_col is None:
        sys.exit(0)
    for batch in table.to_batches(max_chunksize=1000):
        df = batch.to_pandas()
        for _, row in df.iterrows():
            obj = {'prompt': str(row[prompt_col]), 'response': str(row[response_col])}
            # dedup via md5 of normalized content (existing store)
            print(json.dumps(obj, ensure_ascii=False))
except Exception:
    pass
" "$tmpfile" | while IFS= read -r line; do
    # Existing dedup check via lib/dedup.py
    if python3 "$DEDUP_DB" --check "$line" >/dev/null 2>&1; then
      continue
    fi
    echo "$line"
    python3 "$DEDUP_DB" --add "$line" >/dev/null 2>&1 || true
  done

  rm -f "$tmpfile"
}

# Main worker logic
if [[ -f "$SNAPSHOT_FILE" ]]; then
  log "Using snapshot: $SNAPSHOT_FILE"
  mapfile -t FILES < <(python3 -c "import json,sys;print('\n'.join(json.load(open(sys.argv[1]))['files']))" "$SNAPSHOT_FILE")
else
  log "WARNING: No snapshot found at $SNAPSHOT_FILE — falling back to HF API (may hit rate limits)"
  mapfile -t FILES < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree(repo='${HF_REPO#*/}', path='$DATE', repo_type='dataset', recursive=False)
for f in tree:
    if f.type == 'file':
        print(f.rfilename)
" 2>/dev/null || true)
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  log "No files found for date $DATE"
  exit 0
fi

# Deterministic shard assignment (same as existing)
sharded_files=()
for i in "${!FILES[@]}"; do
  if (( i % TOTAL_SHARDS == SHARD_ID )); then
    sharded_files+=("${FILES[$i]}")
  fi
done

log "Shard $SHARD_ID/$TOTAL_SHARDS processing ${#sharded_files[@]} files"

OUTDIR="batches/public-merged/${DATE}"
mkdir -p "$OUTDIR"
TS="$(date -u +%Y%m%d%H%M%S)"
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"

> "$OUTFILE"
for f in "${sharded_files[@]}"; do
  log "Processing:
