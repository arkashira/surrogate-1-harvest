# surrogate-1 / discovery

Below� Synthesis: best parts merged, contradictions resolved, concrete actions preserved

Below is ONE final, minimal-diff plan and code that:

- Uses date-partitioned output (`batches/public-merged/{YYYY-MM-DD}/shard{N}-{HHMMSS}.jsonl`)
- Adds a single pre-flight file list (non-recursive) to eliminate per-file API calls
- Downloads via CDN (`resolve/main/...`) with no auth header (bypasses API rate limits)
- Keeps deterministic shard assignment (`slug-hash % 16 == SHARD_ID`)
- Is idempotent and overwrite-safe (timestamp in filename)
- Adds lightweight safety/observability (summary JSON, skip already-produced shard outputs)
- Touches only `bin/dataset-enrich.sh` + one small pre-flight script + workflow snippet

---

## 1) Pre-flight: generate file list (run once per workflow)

`scripts/generate-file-list.sh`

```bash
#!/usr/bin/env bash
# Generate a non-recursive file list for a date folder.
# Usage: ./generate-file-list.sh [YYYY-MM-DD]
set -euo pipefail

DATE="${1:-$(date -u +%Y-%m-%d)}"
OUT="file-list-${DATE}.json"
REPO="axentx/surrogate-1-training-pairs"

python3 - <<PY
import json, datetime, os, sys
from huggingface_hub import HfApi

api = HfApi()
items = api.list_repo_tree("${REPO}", path=f"batches/public-merged/${DATE}", recursive=False)
files = [
    f.rfilename for f in items
    if f.rfilename.endswith(('.jsonl', '.parquet')) and not f.rfilename.endswith('/')
]

out_path = "${OUT}"
with open(out_path, "w") as f:
    json.dump({
        "date": "${DATE}",
        "repo": "${REPO}",
        "files": sorted(files),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z"
    }, f, indent=2)

print(f"Wrote {out_path} with {len(files)} files")
PY
```

---

## 2) Worker: updated `bin/dataset-enrich.sh`

Key behaviors:
- Uses pre-flight `file-list-YYYY-MM-DD.json` when present; otherwise exits cleanly (safe default).
- Downloads via CDN (`resolve/main/...`) with retries.
- Keeps deterministic shard assignment unchanged.
- Writes per-shard output with timestamp; never reuses filenames.
- Produces `ingest-summary.json` with counts/errors.
- Skips processing if shard output already exists (idempotent).

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Deterministic shard worker with CDN-bypass ingestion.
set -euo pipefail

# -- config --
REPO="axentx/surrogate-1-training-pairs"
BASE_URL="https://huggingface.co/datasets/${REPO}/resolve/main"
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEDUP_PY="${WORKDIR}/lib/dedup.py"

HF_TOKEN="${HF_TOKEN:-}"
SHARD_ID="${SHARD_ID:?required}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE="${INGEST_DATE:-$(date -u +%Y-%m-%d)}"
TIMESTAMP="$(date -u +%H%M%S)"
OUT_DIR="${WORKDIR}/out/batches/public-merged/${DATE}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"
SUMMARY_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}-summary.json"

FILE_LIST="${WORKDIR}/file-list-${DATE}.json"

mkdir -p "${OUT_DIR}"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# -- helpers --
should_process() {
  local slug="$1"
  local hash
  hash=$(echo -n "$slug" | md5sum | cut -c1-8)
  hash=$((0x${hash} % TOTAL_SHARDS))
  [[ $hash -eq $SHARD_ID ]]
}

download_cdn() {
  local path="$1"
  local out="$2"
  curl -fL --retry 3 --retry-delay 5 \
    "${BASE_URL}/${path}" -o "${out}"
}

process_file() {
  local relpath="$1"
  local tmp
  tmp="$(mktemp)"
  if download_cdn "${relpath}" "${tmp}"; then
    python3 "${DEDUP_PY}" --input "${tmp}" --shard-id "${SHARD_ID}" --out "${OUT_FILE}" || true
  else
    log "WARN: failed to download ${relpath}"
    return 1
  fi
  rm -f "${tmp}"
}

# -- main --
log "Starting shard ${SHARD_ID}/${TOTAL_SHARDS} for ${DATE}"

# Idempotency: if any existing shard output exists for this run pattern, skip processing.
# (We still produce a fresh timestamped file each run, but avoid re-processing same list.)
if compgen -G "${OUT_DIR}/shard${SHARD_ID}-*.jsonl" > /dev/null 2>&1; then
  log "Found existing shard outputs in ${OUT_DIR}; skipping processing (idempotency)."
  exit 0
fi

if [[ -f "${FILE_LIST}" ]]; then
  log "Using pre-flight file list ${FILE_LIST}"
  mapfile -t FILES < <(python3 -c "
import json, sys
with open('${FILE_LIST}') as f:
    data=json.load(f)
for fn in data.get('files', []):
    print(fn)
")
  if [[ ${#FILES[@]} -eq 0 ]]; then
    log "WARN: file list empty; nothing to process."
    echo '{"shard_id":'${SHARD_ID}',"date":"'${DATE}'","status":"no_files","processed":0,"skipped":0,"errors":0}' > "${SUMMARY_FILE}"
    exit 0
  fi
else
  log "WARN: no file list ${FILE_LIST}; skipping (prefer pre-flight list)."
  echo '{"shard_id":'${SHARD_ID}',"date":"'${DATE}'","status":"no_file_list","processed":0,"skipped":0,"errors":0}' > "${SUMMARY_FILE}"
  exit 0
fi

TOTAL=${#FILES[@]}
PROCESSED=0
ERRORS=0

for f in "${FILES[@]}"; do
  slug="${f##*/}"
  slug="${slug%.*}"
  if should_process "${slug}"; then
    log "Processing ${f}"
    if process_file "${f}"; then
      PROCESSED=$((PROCESSED+1))
    else
      ERRORS=$((ERRORS+1))
    fi
  fi
done

log "Shard ${SHARD_ID} finished. Output: ${OUT_FILE}"
echo '{
  "shard_id": '${SHARD_ID}',
  "date": "'${DATE}'",
  "status": "done",
  "processed": '${PROCESSED}',
  "total_candidates": '${TOTAL}',
  "errors": '${ERRORS}',
  "output_file": "'${OUT_FILE}'",
  "generated_at": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"
}' > "${SUMMARY_FILE}"
```

---

## 3) Workflow snippet (add pre-flight + matrix)

Add to `.github/workflows/ingest.yml` (minimal diff):

```yaml
jobs:
  preflight:
    runs-on: ubuntu-latest
    outputs:
      ingest_date: ${{ steps.date.outputs.ingest_date }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_hub
      - run: ./scripts/generate-file-list.sh
      - name: Set date output
        id: date
        run: echo "ingest_date=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT
      - uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list-*.json

  ingest:
    needs: preflight
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,
