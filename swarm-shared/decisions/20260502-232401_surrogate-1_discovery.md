# surrogate-1 / discovery

Candidate 3:
## Final Implementation Plan (≤2h)

**Goal:** Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshots + CDN-only fetches to avoid HF API rate limits and schema heterogeneity issues.

### Steps (est. 90–110 min)

1. **Add snapshot utility** (`bin/snapshot.sh`)  
   - Single API call: `list_repo_tree(path, recursive=False)` for today’s folder (or a provided date).  
   - Save flat file list to `snapshots/<date>/file-list.json`.  
   - Exit non-zero if API 429; caller can retry after 360s.

2. **Refactor `bin/dataset-enrich.sh`**  
   - Accept optional `SNAPSHOT_FILE` env var. If absent, run snapshot step once (best-effort; fallback to CDN glob if API fails).  
   - Remove `load_dataset(streaming=True)` and any recursive listing.  
   - For each file in snapshot:  
     - Download via CDN URL `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>` (no auth header).  
     - Stream-parse with `pyarrow`/`jsonl` and project to `{prompt, response}` only.  
     - Compute md5, dedup via `lib/dedup.py`.  
   - Emit `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

3. **Update GitHub Actions matrix** (`/.github/workflows/ingest.yml`)  
   - Add an initial “snapshot” job that runs once per workflow and uploads `file-list.json` as an artifact.  
   - Pass `SNAPSHOT_FILE` (downloaded artifact) to each shard runner so all 16 workers use identical file list.  
   - Keep matrix strategy `shard: [0..15]`.

4. **Add lightweight fallback**  
   - If snapshot fails (API 429), workers fall back to CDN glob for the target date folder (safe because CDN limits are much higher).  
   - Log warning; continue. Never block ingestion.

5. **Validation & smoke test**  
   - Run `bin/snapshot.sh` locally (or via `gh workflow run`).  
   - Run one shard manually with `HF_TOKEN=xxx SNAPSHOT_FILE=snapshots/2026-05-02/file-list.json bash bin/dataset-enrich.sh`.  
   - Confirm output file shape and dedup behavior.

---

## Code Snippets

### 1. Snapshot utility (`bin/snapshot.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots/${DATE}"
OUTFILE="${OUTDIR}/file-list.json"

mkdir -p "${OUTDIR}"

echo "[$(date -u)] Listing ${REPO} tree for ${DATE} ..."
# Single non-recursive API call per folder (avoids 100x pagination)
# If DATE is a folder prefix, list that folder; else list root.
if curl -s -H "Authorization: Bearer ${HF_TOKEN:-}" \
  "https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE}&recursive=false" \
  | jq -c '.' > "${OUTFILE}.tmp"; then
  mv "${OUTFILE}.tmp" "${OUTFILE}"
  COUNT=$(jq 'length' "${OUTFILE}")
  echo "[$(date -u)] Snapshot saved: ${OUTFILE} (${COUNT} files)"
else
  echo "[$(date-u)] API error or 429 — snapshot failed" >&2
  rm -f "${OUTFILE}.tmp"
  exit 1
fi
```

### 2. Updated `bin/dataset-enrich.sh` (core changes)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

OUTDIR="batches/public-merged/${DATE}"
TS=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"
mkdir -p "${OUTDIR}"

# Dedup helper
DEDUPE_PY="lib/dedup.py"

# Resolve file list
resolve_files() {
  if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
    # Use pre-computed snapshot (preferred)
    jq -r --arg d "${DATE}" '
      .[] | select(.path | startswith($d + "/") or .path == $d or ($d == ""))
      | .path' "${SNAPSHOT_FILE}"
  else
    # Fallback: CDN glob via repo tree API (non-recursive per folder)
    # If DATE is a folder, list it; else list root.
    curl -s -H "Authorization: Bearer ${HF_TOKEN:-}" \
      "https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE}&recursive=false" \
      | jq -r '.[].path'
  fi
}

# Deterministic shard assignment by slug hash
assign_shard() {
  local path="$1"
  # Stable hash -> bucket
  local hash
  hash=$(echo -n "${path}" | md5sum | cut -c1-8)
  local bucket=$((0x${hash} % TOTAL_SHARDS))
  echo "${bucket}"
}

process_file() {
  local path="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"

  # Stream download + parse; project to {prompt,response}
  # Supports .jsonl and .parquet via python helper (kept minimal here)
  # For .parquet we use a small python shim to avoid heavy deps in bash.
  case "${path}" in
    *.jsonl)
      curl -sL "${url}" | while IFS= read -r line; do
        # Lightweight projection: try to extract prompt/response fields
        prompt=$(echo "${line}" | jq -r '.prompt // .instruction // .input // empty' 2>/dev/null || true)
        response=$(echo "${line}" | jq -r '.response // .output // empty' 2>/dev/null || true)
        if [[ -n "${prompt}" && -n "${response}" ]]; then
          jq -nc --arg p "${prompt}" --arg r "${response}" '{prompt:$p, response:$r}'
        fi
      done
      ;;
    *.parquet)
      python3 -c "
import sys, pyarrow.parquet as pq, json, io, urllib.request
url = '${url}'
with urllib.request.urlopen(url) as f:
    table = pq.read_table(io.BytesIO(f.read()))
    for b in table.to_batches():
        df = b.to_pandas()
        for _, row in df.iterrows():
            prompt = row.get('prompt') or row.get('instruction') or row.get('input')
            response = row.get('response') or row.get('output')
            if prompt and response:
                print(json.dumps({'prompt': str(prompt), 'response': str(response)}))
"
      ;;
  esac
}

# Main
resolve_files | while read -r path; do
  [[ -z "${path}" ]] && continue
  if [[ "$(assign_shard "${path}")" != "${SHARD_ID}" ]]; then
    continue
  fi
  echo "[$(date -u)] Processing ${path}..."
  process_file "${path}" | "${DEDUPE_PY}" >> "${OUTFILE}"
done

echo "[$(date -u)] Shard ${SHARD_ID} complete: ${OUTFILE}"
```

### 3. GitHub Actions workflow (ingest.yml)

```yml
name: Ingest

on:
  workflow_dispatch:
  schedule:
    - cron: '*/30 * * * *'

jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot_file: ${{ steps.set.outputs.snapshot_file }}
    steps:
      - uses: actions/checkout@
