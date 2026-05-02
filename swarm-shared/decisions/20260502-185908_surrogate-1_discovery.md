# surrogate-1 / discovery

## Highest-value incremental improvement (≤2h)
Ship deterministic date-partitioned ingestion with CDN-bypass and a pre-flight file list to eliminate redundant API calls, overwrite races, and rate-limit exposure.

- **Why**: Eliminates HF API rate-limit pressure during training, prevents shard overwrite races, and makes re-runs safe and reproducible.
- **Scope**: `bin/dataset-enrich.sh` + `.github/workflows/ingest.yml` + small Python helper for file-list generation.
- **Deliverables**:
  1. Pre-flight file list (JSON) generated once per run date and embedded in the workflow.
  2. Date-partitioned output path: `batches/public-merged/YYYY/MM/DD/shard<N>-<HHMMSS>.jsonl`.
  3. CDN-only fetches during ingestion (no Authorization header; use `resolve/main/` URLs).
  4. Deterministic shard assignment via `slug-hash % 16` so re-runs with the same list produce identical shard mapping.

---

## Concrete implementation plan (≤2h)

### 1) Add pre-flight file-list generator (run on Mac/CI before matrix)
- Single API call to list top-level date folder(s) or repo root (non-recursive) and persist to `file-list-YYYY-MM-DD.json`.
- Embed path in workflow via `env.FILE_LIST` or upload/download artifact.

### 2) Update `bin/dataset-enrich.sh`
- Accept `FILE_LIST` (JSON) and `SHARD_ID` (0–15) as env inputs.
- Compute deterministic shard: `hash(slug) % 16` → assign file to shard.
- Fetch each assigned file via CDN URL:
  ```
  https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>
  ```
- Parse, normalize, dedup (existing `lib/dedup.py`), emit JSONL.
- Output to:
  ```
  batches/public-merged/YYYY/MM/DD/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl
  ```

### 3) Update `.github/workflows/ingest.yml`
- Add `run_date` output (UTC `YYYY-MM-DD`) and pass to matrix job.
- Add step to generate/artifact `file-list-<run_date>.json` (or compute once and embed if list is small).
- Matrix job: `shard: [0..15]` with `env.FILE_LIST`, `env.RUN_DATE`, `env.SHARD_ID`.
- Push outputs to deterministic date-partitioned paths.

### 4) (Optional) Reuse existing HF token behavior
- Keep `HF_TOKEN` for push; do not use token for CDN downloads.

---

## Code snippets

### bin/dataset-enrich.sh (updated)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${HF_TOKEN:?Need HF_TOKEN}"
: "${HF_REPO:?Need HF_REPO, e.g. axentx/surrogate-1-training-pairs}"
: "${SHARD_ID:?Need SHARD_ID 0-15}"
: "${FILE_LIST:?Need path to file-list JSON}"
: "${RUN_DATE:?Need RUN_DATE YYYY-MM-DD}"

# Paths
OUT_DIR="batches/public-merged/$(echo "$RUN_DATE" | tr '-' '/')"
TS=$(date -u +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
mkdir -p "$(dirname "$OUT_FILE")"

echo "[$(date -u)] Shard ${SHARD_ID} starting; output -> ${OUT_FILE}"

# Deterministic shard assignment helper
shard_for_slug() {
  local slug="$1"
  # Use deterministic numeric hash (same across runs)
  local hash
  hash=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( hash % 16 ))
}

# Process assigned files
tmp_out=$(mktemp)
trap 'rm -f "$tmp_out"' EXIT

jq -r '.[]' "$FILE_LIST" | while IFS= read -r relpath; do
  slug=$(basename "$relpath" | sed 's/\.[^.]*$//')
  if [[ $(shard_for_slug "$slug") -ne "$SHARD_ID" ]]; then
    continue
  fi

  # CDN bypass: no auth header
  url="https://huggingface.co/datasets/${HF_REPO}/resolve/main/${relpath}"
  echo "[$(date -u)] Fetching ${relpath} -> shard ${SHARD_ID}"

  # Download and parse (project to {prompt,response} only)
  # Replace this block with your schema-specific parser.
  # Example using python for safety:
  python3 - "$url" "$tmp_out" <<'PY'
import sys, json, urllib.request, tempfile, os
from pathlib import Path

url = sys.argv[1]
out_f = sys.argv[2]

try:
    with urllib.request.urlopen(url) as resp:
        raw = resp.read()
except Exception as e:
    sys.stderr.write(f"Failed to fetch {url}: {e}\n")
    sys.exit(0)

# Placeholder: detect by extension and project to {prompt,response}
# For .jsonl:
if url.endswith('.jsonl'):
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get('prompt') or obj.get('input') or obj.get('text') or ''
        response = obj.get('response') or obj.get('output') or ''
        if prompt or response:
            with open(out_f, 'a', encoding='utf-8') as f:
                json.dump({'prompt': prompt, 'response': response}, f, ensure_ascii=False)
                f.write('\n')
# For .parquet: use pyarrow projection (not shown)
else:
    # fallback: skip
    pass
PY

done

# Dedup via central store (existing lib/dedup.py)
if [[ -f lib/dedup.py ]]; then
  python3 lib/dedup.py --input "$tmp_out" --output "$OUT_FILE"
else
  cp "$tmp_out" "$OUT_FILE"
fi

echo "[$(date -u)] Shard ${SHARD_ID} finished: ${OUT_FILE}"
```

### .github/workflows/ingest.yml (updated)
```yaml
name: Ingest (16-shard)

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

env:
  HF_REPO: axentx/surrogate-1-training-pairs

jobs:
  prepare:
    runs-on: ubuntu-latest
    outputs:
      run_date: ${{ steps.date.outputs.run_date }}
      file_list: ${{ steps.filelist.outputs.file_list }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set run date (UTC)
        id: date
        run: echo "run_date=$(date -u +%Y-%m-%d)" >> "$GITHUB_OUTPUT"

      - name: Generate file list (single API call)
        id: filelist
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          # List non-recursive top-level or date folder for today; adapt as needed.
          # This avoids recursive pagination and rate-limits.
          python3 -c "
import os, json, sys
from huggingface_hub import HfApi
api = HfApi(token=os.getenv('HF_TOKEN'))
repo = os.getenv('HF_REPO')
# Example: list today's folder (YYYY-MM-DD) or root
items = api.list_repo_tree(repo=repo, path='', recursive=False)
files = [i.rfilename for i in items if i.type == 'file']
with open('file-list.json', 'w') as f:
    json.dump(files, f)
"
          echo "file_list=$(pwd)/file-list.json" >> "$GITHUB_OUTPUT"

      - name: Upload file list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list-${{ steps.date.outputs.run_date }}
          path: file-list.json

  shard:
    needs: prepare
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [0,1,2
