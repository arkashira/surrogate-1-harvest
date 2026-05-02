# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_files` in `bin/dataset-enrich.sh` with a deterministic pre-flight snapshot + CDN-only fetches. This eliminates HF API rate limits (429) and pyarrow CastError on mixed-schema repos while preserving the 16-shard parallel ingest architecture.

### Steps (1h 30m total)

1. **Add snapshot generator** (`bin/snapshot.sh`) — run once per date folder from Mac (15m)  
   - Uses `list_repo_tree(path, recursive=False)` per date folder  
   - Outputs `snapshot-<date>.json` containing `{file,sha,size}` for every parquet/jsonl in that folder  
   - Embeds snapshot into runner via artifact or inline JSON

2. **Update `bin/dataset-enrich.sh`** (45m)  
   - Accept snapshot JSON (file or env) and `SHARD_ID`/`TOTAL_SHARDS`  
   - Deterministic shard assignment: `hash(slug) % TOTAL_SHARDS == SHARD_ID`  
   - Fetch via CDN: `curl -L "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>"`  
   - Stream-parse with `pyarrow`/`pandas` projecting only `{prompt,response}`; drop all other columns  
   - Dedup via central `lib/dedup.py` (md5 store)  
   - Write `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`

3. **Update GitHub Actions matrix** (15m)  
   - Generate snapshot in a prior job (or reuse cached snapshot)  
   - Pass snapshot as artifact to 16 shard jobs  
   - Keep `HF_TOKEN` only for final push (no read API calls)

4. **Add fallback/retry** (15m)  
   - On CDN 404/5xx: exponential backoff, skip file, log  
   - After 429 on initial snapshot: wait 360s, retry once

---

### Code Snippets

#### 1. Snapshot generator (run from Mac)

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-02 > snapshot-2026-05-02.json

set -euo pipefail
REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%F)}"
API="https://huggingface.co/api/datasets/${REPO}/tree/main"

# Single API call per date folder (non-recursive)
# If folder has subfolders, call per subfolder (still bounded)
curl -s -H "Authorization: Bearer ${HF_TOKEN}" \
  "${API}?path=batches/public-merged/${DATE}&recursive=false" |
  jq '[ .[] | select(.type=="file") | {file: .path, sha: .oid, size: .size} ]'
```

#### 2. Updated dataset-enrich.sh (core worker)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Runs in GitHub Actions shard job
set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%F)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
SNAPSHOT_JSON="${1:-}"  # path to snapshot JSON

if [[ -z "${SNAPSHOT_JSON}" || ! -f "${SNAPSHOT_JSON}" ]]; then
  echo "ERROR: snapshot JSON not found" >&2
  exit 1
fi

# Deterministic shard assignment
function shard_for() {
  local slug="$1"
  # deterministic hash across runners
  local h=$(echo -n "$slug" | sha256sum | tr -d ' -' | head -c 16)
  local n=$(( 0x${h} % TOTAL_SHARDS ))
  echo "$n"
}

# Dedup helper
function is_duplicate() {
  local md5="$1"
  python3 lib/dedup.py check "$md5"
}

function mark_seen() {
  local md5="$1"
  python3 lib/dedup.py add "$md5"
}

# Process files assigned to this shard
processed=0
skipped=0
uploaded=0

output_dir="batches/public-merged/${DATE}"
mkdir -p "$output_dir"
ts=$(date +%H%M%S)
outfile="${output_dir}/shard${SHARD_ID}-${ts}.jsonl"
: > "$outfile"

while IFS= read -r entry; do
  file=$(echo "$entry" | jq -r '.file')
  sha=$(echo "$entry" | jq -r '.sha')
  slug=$(basename "$file" | sed 's/\.[^.]*$//')

  s=$(shard_for "$slug")
  if [[ "$s" != "$SHARD_ID" ]]; then
    continue
  fi

  # CDN fetch (no Authorization header required for public files)
  url="https://huggingface.co/datasets/${REPO}/resolve/main/${file}"
  tmp=$(mktemp)
  retry=0
  max_retry=3
  ok=false
  while (( retry < max_retry )); do
    if curl -fsSL --retry 2 --retry-delay 1 -o "$tmp" "$url"; then
      ok=true
      break
    fi
    ((retry++))
    sleep $(( 2 ** retry ))
  done

  if ! $ok; then
    echo "WARN: failed to fetch ${file}" >&2
    ((skipped++)) || true
    rm -f "$tmp"
    continue
  fi

  # Project to {prompt,response} only; ignore extra columns
  # Supports .jsonl and .parquet
  python3 <<PY >> "$outfile"
import sys, json, pyarrow.parquet as pq, pyarrow as pa, pandas as pd, os, hashlib, tempfile

tmp = "${tmp}"
outfile = "${outfile}"

def hash_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

def extract_pairs(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.parquet':
            tbl = pq.read_table(path, columns=['prompt', 'response'])
            df = tbl.to_pandas()
        else:
            # assume jsonl
            df = pd.read_json(path, lines=True, dtype=str)
            if 'prompt' not in df.columns or 'response' not in df.columns:
                # best-effort: find columns that look like prompt/response
                cols = [c for c in df.columns if 'prompt' in str(c).lower()]
                resp = [c for c in df.columns if 'response' in str(c).lower() or 'completion' in str(c).lower()]
                if cols and resp:
                    df = df.rename(columns={cols[0]: 'prompt', resp[0]: 'response'})
                else:
                    return
            # keep only prompt/response
            df = df[['prompt', 'response']].fillna('')
        for _, row in df.iterrows():
            prompt = str(row['prompt']).strip()
            response = str(row['response']).strip()
            if not prompt or not response:
                continue
            pair = {'prompt': prompt, 'response': response}
            md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            print(json.dumps({'pair': pair, 'md5': md5}))
    except Exception as e:
        # skip malformed files
        sys.stderr.write(f'WARN: parse error {path}: {e}\\n')

extract_pairs(tmp)
PY

  # Dedup and finalize
  while IFS= read -r line; do
    md5=$(echo "$line" | jq -r '.md5')
    if is_duplicate "$md5"; then
      continue
    fi
    pair=$(echo "$line" | jq -c '.
