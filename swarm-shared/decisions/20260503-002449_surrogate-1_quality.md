# surrogate-1 / quality

## Implementation Plan — CDN-first snapshot + zero-HF-API ingestion

**Highest-value change (≤2h)**: Add `bin/snapshot.sh` that produces a deterministic file manifest per date folder and update ingestion/training to use CDN URLs exclusively when a snapshot is provided. This eliminates HuggingFace API calls during training and removes 429/128-hr-commit bottlenecks from the hot path.

### Why this now
- Training repeatedly hits HF API (`list_repo_files`/`load_dataset`) → 429s and ingestion stalls.
- Public dataset files are already CDN-accessible with no auth and much higher limits.
- A single Mac-side snapshot (JSON) is cheap, deterministic, and embeddable in training scripts.
- Fits existing patterns: pre-list once, embed in train.py; Lightning Studio reuse; CDN-only fetches.

---

### Concrete steps (all in `/opt/axentx/surrogate-1`)

#### 1) Add `bin/snapshot.sh`
Produces `snapshot-<date>.json` containing `{repo, path, url, size, etag_hint}` for every file in a date folder. Uses `list_repo_tree` (non-recursive per subfolder) to minimize API calls and avoids recursive `list_repo_files`.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-01
# Output: snapshots/snapshot-2026-05-01.json

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTDIR="./snapshots"
OUTFILE="${OUTDIR}/snapshot-${DATE}.json"
HF_API="https://huggingface.co/api"

mkdir -p "${OUTDIR}"

echo "Creating snapshot for ${REPO} path='${DATE}' -> ${OUTFILE}"

# Top-level tree for the date folder (non-recursive)
# If folder is large, tree may paginate; handle Link header simply by retries.
# We'll use hubtools or curl; prefer curl for portability.
fetch_tree() {
  local path="${1}"
  curl -sSfL \
    -H "Authorization: Bearer ${HF_TOKEN:-}" \
    "${HF_API}/datasets/${REPO}/tree?path=${path}&recursive=false"
}

# Encode path for JSON string
jq_escape() { printf '%s' "$1" | jq -Rs .; }

# Start JSON array
echo "[" > "${OUTFILE}.tmp"

first=true
page=1
while :; do
  resp=$(fetch_tree "${DATE}")
  # If empty or not array, break
  if ! echo "${resp}" | jq -e . >/dev 2>&1; then
    echo "No valid response (possibly empty folder or rate-limited). Retrying once..."
    sleep 5
    resp=$(fetch_tree "${DATE}")
  fi

  count=$(echo "${resp}" | jq 'length')
  if [ "${count}" -eq 0 ]; then
    break
  fi

  # Process entries
  for i in $(seq 0 $((count - 1))); do
    type=$(echo "${resp}" | jq -r ".[${i}].type")
    entry_path=$(echo "${resp}" | jq -r ".[${i}].path")

    # If it's a subfolder, recurse one level only (avoid deep recursion in API)
    if [ "${type}" = "tree" ]; then
      subresp=$(curl -sSfL \
        -H "Authorization: Bearer ${HF_TOKEN:-}" \
        "${HF_API}/datasets/${REPO}/tree?path=${entry_path}&recursive=false")
      subcount=$(echo "${subresp}" | jq 'length')
      for j in $(seq 0 $((subcount - 1))); do
        stype=$(echo "${subresp}" | jq -r ".[${j}].type")
        spath=$(echo "${subresp}" | jq -r ".[${j}].path")
        if [ "${stype}" = "blob" ]; then
          url="https://huggingface.co/datasets/${REPO}/resolve/main/${spath}"
          size=$(echo "${subresp}" | jq -r ".[${j}].size // 0")
          if [ "${first}" = true ]; then first=false; else echo "," >> "${OUTFILE}.tmp"; fi
          jq -n \
            --arg repo "${REPO}" \
            --arg path "${spath}" \
            --arg url "${url}" \
            --argjson size "${size}" \
            '{repo:$repo, path:$path, url:$url, size:$size}' >> "${OUTFILE}.tmp"
        fi
      done
    elif [ "${type}" = "blob" ]; then
      url="https://huggingface.co/datasets/${REPO}/resolve/main/${entry_path}"
      size=$(echo "${resp}" | jq -r ".[${i}].size // 0")
      if [ "${first}" = true ]; then first=false; else echo "," >> "${OUTFILE}.tmp"; fi
      jq -n \
        --arg repo "${REPO}" \
        --arg path "${entry_path}" \
        --arg url "${url}" \
        --argjson size "${size}" \
        '{repo:$repo, path:$path, url:$url, size:$size}' >> "${OUTFILE}.tmp"
    fi
  done

  # Simple pagination: if fewer than 100 entries returned, assume last page.
  # HF tree pagination uses `next` link; for safety we break here (non-recursive per folder is cheap).
  if [ "${count}" -lt 100 ]; then
    break
  else
    page=$((page + 1))
    echo "Large folder page ${page}, continuing..."
    sleep 2
  fi
done

echo "]" >> "${OUTFILE}.tmp"

# Validate JSON and finalize
if jq -e . "${OUTFILE}.tmp" >/dev/null 2>&1; then
  mv "${OUTFILE}.tmp" "${OUTFILE}"
  entries=$(jq 'length' "${OUTFILE}")
  total_size=$(jq '[.[].size] | add // 0' "${OUTFILE}")
  echo "Snapshot written: ${OUTFILE} (${entries} files, total ${total_size} bytes)"
else
  echo "ERROR: Invalid JSON produced"
  rm -f "${OUTFILE}.tmp"
  exit 1
fi
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

#### 2) Add `bin/ingest-cdn.sh` (lightweight wrapper for GitHub Actions runners)
Uses snapshot JSON to download via CDN only; projects to `{prompt,response}` and outputs NDJSON. Keeps existing dedup behavior via `lib/dedup.py`.

```bash
#!/usr/bin/env bash
# bin/ingest-cdn.sh
# Usage: SHARD_ID=0 HF_TOKEN=... ./bin/ingest-cdn.sh snapshot-2026-05-01.json
# Reads snapshot JSON, filters by SHARD_ID (hash-based sharding), downloads via CDN,
# projects to {prompt,response}, and outputs NDJSON lines to stdout.

set -euo pipefail

SNAPSHOT_FILE="${1:?Snapshot JSON file required}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

if [ ! -f "${SNAPSHOT_FILE}" ]; then
  echo "ERROR: Snapshot file not found: ${SNAPSHOT_FILE}" >&2
  exit 1
fi

# Deterministic shard assignment by path hash
shard_for_path() {
  local path="$1"
  # Use deterministic numeric hash (compatible across runs)
  local hash
  hash=$(echo -n "${path}" | cksum | awk '{print $1}')
  echo $(( hash % TOTAL_SHARDS ))
}

# Project file to prompt/response (schema-aware projection)
# Supports common patterns: {prompt,response}, {input,output}, {question,answer}
project_record() {
  local file="$1"
  local tmp
  tmp=$(mktemp)
  # Try parquet -> json projection; fallback to jsonl
  if [[ "${file}" == *.parquet ]]; then
    python3 -c "
import sys, pyarrow.parquet as pq, json
tbl = pq.read_table(sys.argv[1])
cols = tbl.column_names
# Try candidate pairs
