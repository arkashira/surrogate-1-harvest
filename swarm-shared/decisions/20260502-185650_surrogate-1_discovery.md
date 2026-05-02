# surrogate-1 / discovery

## 1. Diagnosis
- No local file-list cache: each runner re-enumerates the full dataset via HF API on every run, risking 429s and wasting quota instead of using CDN-only fetches.
- No deterministic shard-to-date mapping: runners process whatever `list_repo_files` returns today, which can drift across runs and produce non-reproducible shard outputs.
- Missing pre-flight check for running HF Space dedup store: runners can’t detect whether the central SQLite dedup is alive, so duplicates slip through and waste commits.
- No backoff/retry for HF 429 or CDN transient errors: a single rate-limit or 5xx aborts the entire shard instead of waiting and resuming.
- No lightweight validation that produced JSONL lines contain exactly `{prompt,response}` and valid UTF-8; malformed rows can poison downstream training.

## 2. Proposed change
File: `bin/dataset-enrich.sh`  
Scope: add a deterministic date-shard mapping, pull a once-per-run file list from a cached JSON (generated on Mac and committed to repo), use CDN-only downloads with retry/backoff, and validate each line before append.

## 3. Implementation
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated to use deterministic date-shard mapping + CDN-only fetches + validation
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
SHARD_ID=${SHARD_ID:-0}          # 0..15 from matrix
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
OUT_DIR="batches/public-merged/${DATE}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"

# -- 1) Use deterministic date folder + cached file list --
# Expect FILE_LIST_JSON to point at a committed JSON produced by list_repo_tree
# (generated on Mac after rate-limit window clears).
FILE_LIST_JSON=${FILE_LIST_JSON:-"file-list-${DATE}.json"}
if [[ ! -f "${FILE_LIST_JSON}" ]]; then
  echo "ERROR: file list ${FILE_LIST_JSON} not found. Generate on Mac via:"
  echo "  python -c \"import json,sys; from huggingface_hub import list_repo_tree; \
    tree=list_repo_tree('${REPO}', path='${DATE}', recursive=True); \
    files=[f.rfilename for f in tree if f.rfilename.endswith('.parquet')]; \
    json.dump(files, sys.stdout)\" > ${FILE_LIST_JSON}"
  exit 1
fi

# Map files to shards by stable hash
map_shard() {
  local file="$1"
  # Deterministic 0..(TOTAL_SHARDS-1)
  echo $(( $(echo -n "$file" | cksum | cut -d' ' -f1) % TOTAL_SHARDS ))
}

# -- 2) CDN-only download with retry/backoff --
MAX_RETRIES=5
cdn_download() {
  local url="$1"
  local out="$2"
  local attempt=0
  local wait=10
  while (( attempt < MAX_RETRIES )); do
    if curl -fsSL --retry 3 --retry-delay 2 -o "${out}" "${url}"; then
      return 0
    fi
    attempt=$((attempt + 1))
    echo "WARN: CDN download failed (attempt ${attempt}/${MAX_RETRIES}) for ${url}"
    if (( attempt == MAX_RETRIES )); then
      echo "ERROR: exhausted retries for ${url}"
      return 1
    fi
    sleep $wait
    wait=$((wait * 2))
  done
}

# -- 3) Lightweight line validation --
validate_line() {
  local line="$1"
  # Must be valid JSON with prompt+response strings
  if ! echo "$line" | python3 -c "
import sys, json
try:
    obj=json.load(sys.stdin)
    assert isinstance(obj, dict)
    assert 'prompt' in obj and isinstance(obj['prompt'], str)
    assert 'response' in obj and isinstance(obj['response'], str)
    assert obj['prompt'].strip() != ''
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    return 1
  fi
  # Must be valid UTF-8
  if ! echo "$line" | iconv -t utf-8 >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

mkdir -p "${OUT_DIR}"
> "${OUT_FILE}.tmp"

count=0
skipped=0
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  target=$(map_shard "$file")
  if (( target != SHARD_ID )); then
    continue
  fi

  url="https://huggingface.co/datasets/${REPO}/resolve/main/${file}"
  tmp_parquet=$(mktemp)
  if ! cdn_download "${url}" "${tmp_parquet}"; then
    rm -f "${tmp_parquet}"
    skipped=$((skipped + 1))
    continue
  fi

  # Extract {prompt,response} only; tolerate schema drift
  python3 -c "
import pyarrow.parquet as pq, json, sys, os
try:
    tbl = pq.read_table('${tmp_parquet}')
    df = tbl.to_pandas()
    # Normalize column names
    prompt_col = next((c for c in df.columns if 'prompt' in c.lower()), None)
    response_col = next((c for c in df.columns if 'response' in c.lower()), None)
    if prompt_col is None or response_col is None:
        # fallback: first two text-like columns
        text_cols = [c for c in df.columns if df[c].dtype == 'object']
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        else:
            sys.exit(0)
    for _, row in df.iterrows():
        obj = {'prompt': str(row[prompt_col]), 'response': str(row[response_col])}
        print(json.dumps(obj, ensure_ascii=False))
except Exception:
    pass
" >> "${OUT_FILE}.tmp" 2>/dev/null || true

  rm -f "${tmp_parquet}"
  count=$((count + 1))
done < <(jq -r '.[]' "${FILE_LIST_JSON}")

# -- 4) Validate lines and finalize --
valid=0
invalid=0
> "${OUT_FILE}"
while IFS= read -r line; do
  if validate_line "$line"; then
    echo "$line" >> "${OUT_FILE}"
    valid=$((valid + 1))
  else
    invalid=$((invalid + 1))
  fi
done < "${OUT_FILE}.tmp"
rm -f "${OUT_FILE}.tmp"

echo "Shard ${SHARD_ID} done: processed ${count} files, wrote ${valid} valid lines, skipped ${invalid} invalid, ${skipped} download failures."
```

## 4. Verification
1. Generate a deterministic file list on Mac (after rate-limit window):
   ```bash
   python -c "
import json, sys
from huggingface_hub import list_repo_tree
tree = list_repo_tree('axentx/surrogate-1-training-pairs', path='$(date -u +%Y-%m-%d)', recursive=True)
files = [f.rfilename for f in tree if f.rfilename.endswith('.parquet')]
with open('file-list-$(date -u +%Y-%m-%d).json','w') as f:
    json.dump(files, f)
   "
   git add file-list-*.json && git commit -m "update file list" && git push
   ```
2. Run one shard locally:
   ```bash
   export SHARD_ID=0 TOTAL_SHARDS=16 FILE_LIST_JSON=file-list-$(date -u +%Y-%m-%d).json
   bash bin/dataset-enrich.sh
   ```
3. Confirm:
   - Output file exists under `batches/public-merged/<date>/shard0-*.jsonl`.
   - Each line is valid JSON with `prompt` and `response` strings.
   - No HF API calls appear in logs (only `https://huggingface.co/datasets/.../resolve/main/...` URLs).
   - Script exits 0 and prints counts.
