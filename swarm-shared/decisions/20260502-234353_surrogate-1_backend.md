# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit risk during ingestion and aligns with the key training insight (single API call → JSON manifest → CDN-only fetches).

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `list_repo_tree` (non-recursive per folder) or `list_repo_files` once, saves to `snapshot/<date>/file-manifest.json`.
   - Deterministic sort for stable shard assignment.
   - Respects 429: wait 360s, retry with exponential backoff.

2. **Update `bin/dataset-enrich.sh`** (30m)  
   - Accept optional manifest path; if provided, iterate manifest entries instead of calling `list_repo_files` per shard.
   - Keep existing 1/16 shard hash logic (`slug-hash % 16 == SHARD_ID`).
   - Download via CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no auth header.

3. **Update GitHub Actions matrix** (20m)  
   - Add a pre-job step that runs `snapshot.sh` and uploads manifest as an artifact.
   - Pass manifest path to each shard via `matrix.manifest_url` or shared workspace file.
   - Ensure `HF_TOKEN` only used for snapshot + final upload (not during CDN downloads).

4. **Add lightweight dedup cache warm-start** (20m)  
   - Download central SQLite dedup store from HF Space (or S3) before processing to reduce cross-run duplicates (best-effort; not required for correctness).

5. **Validation + smoke test** (20m)  
   - Run locally with a small repo subset to verify manifest generation, shard assignment, CDN download, and upload path format.

---

## Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate deterministic file manifest for axentx/surrogate-1-training-pairs
# Usage: HF_TOKEN=... bin/snapshot.sh [--date YYYY-MM-DD] [--repo owner/repo]

set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUTDIR="snapshot/${DATE}"
MANIFEST="${OUTDIR}/file-manifest.json"
HF_API="https://huggingface.co/api"
MAX_RETRIES=5
BASE_WAIT=10

mkdir -p "${OUTDIR}"

# List all files (non-recursive per folder) with retries
list_files() {
  local path="${1:-}"
  local retries=0
  while true; do
    if resp=$(curl -sSf --retry "${MAX_RETRIES}" \
      -H "Authorization: Bearer ${HF_TOKEN}" \
      "${HF_API}/datasets/${REPO}/tree${path:+?path=${path}}" 2>&1); then
      echo "${resp}" | jq -r '.[].path' 2>/dev/null || true
      return 0
    else
      if echo "${resp}" | grep -q "429"; then
        if (( retries >= MAX_RETRIES )); then
          echo "Rate-limited after retries; waiting 360s" >&2
          sleep 360
          retries=0
          continue
        fi
        wait=$(( BASE_WAIT * 2 ** retries ))
        echo "Rate-limited; retry $((retries+1)) in ${wait}s" >&2
        sleep "${wait}"
        ((retries++)) || true
        continue
      else
        echo "Failed to list tree: ${resp}" >&2
        return 1
      fi
    fi
  done
}

# Collect recursively by walking top-level folders
all_files=()
folders=("" "batches" "raw" "enriched")  # adjust if needed
for folder in "${folders[@]}"; do
  while IFS= read -r line; do
    [[ -n "${line}" ]] && all_files+=("${line}")
  done < <(list_files "${folder}")
done

# Dedupe and sort for deterministic shard assignment
printf '%s\n' "${all_files[@]}" | sort -u | jq -R -s -c 'split("\n") | map(select(. != ""))' > "${MANIFEST}"
echo "Manifest written: ${MANIFEST} ($(jq length "${MANIFEST}") files)"
```

### 2. `bin/dataset-enrich.sh` (updated core loop)
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated to accept manifest for CDN-only downloads

set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
MANIFEST="${MANIFEST:-}"  # optional path to file-manifest.json
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUTDIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTPUT="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "${OUTDIR}"

# Deterministic shard assignment by slug hash
slug_hash() {
  echo -n "$1" | sha256sum | tr -d ' -' | head -c 16 | xargs -I{} printf '%d' "0x{}"
}

process_file() {
  local path="$1"
  local cdn_url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"
  # Download via CDN (no auth) and project to {prompt,response}
  # Replace with actual schema handling per surrogate-1 rules
  if tmp=$(mktemp); then
    curl -sSf -o "${tmp}" "${cdn_url}" || { echo "Download failed: ${cdn_url}" >&2; return 1; }
    # Placeholder: parse and normalize to {prompt,response}
    # For parquet: use python helper; for jsonl: jq
    # Emit one JSON object per line to stdout
    python3 -c "
import sys, pyarrow.parquet as pq, json, os
try:
    t = pq.read_table('${tmp}')
    for i in range(t.num_rows):
        row = {k: t[k][i].as_py() for k in t.column_names}
        # Project to prompt/response only
        prompt = row.get('prompt') or row.get('text') or ''
        response = row.get('response') or row.get('completion') or ''
        if prompt and response:
            print(json.dumps({'prompt': str(prompt), 'response': str(response)}))
except Exception as e:
    sys.stderr.write(f'Parse error: {e}\\n')
" 2>/dev/null || true
    rm -f "${tmp}"
  fi
}

# Decide source list
if [[ -n "${MANIFEST}" && -f "${MANIFEST}" ]]; then
  mapfile -t FILES < <(jq -r '.[]' "${MANIFEST}")
else
  # Fallback: list repo files (may hit rate limits)
  mapfile -t FILES < <(curl -sSf -H "Authorization: Bearer ${HF_TOKEN}" \
    "https://huggingface.co/api/datasets/${REPO}/files" | jq -r '.[].path')
fi

# Process shard slice
for path in "${FILES[@]}"; do
  h=$(slug_hash "${path}")
  if (( h % TOTAL_SHARDS == SHARD_ID )); then
    process_file "${path}" >> "${OUTPUT}"
  fi
done

echo "Shard ${SHARD_ID} output: ${OUTPUT}"
```

### 3. GitHub Actions update (`.github/workflows/ingest.yml` snippet)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      manifest-path: ${{ steps.manifest.outputs.path }}
    steps:
      - uses: actions/checkout@v4
      - name: Generate manifest
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          chmod +x bin/snapshot.sh
          bin/snapshot.sh --date $(date +
