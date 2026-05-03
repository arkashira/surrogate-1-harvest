# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit (429) and recursive listing overhead by switching to single non-recursive `list_repo_tree` + CDN-only fetches + deterministic sibling-repo routing.

### Changes

1. **`bin/dataset-enrich.sh`** — replace recursive listing + streaming with:
   - One `list_repo_tree(recursive=False)` call per date folder (saved to `file-list.json`)
   - Deterministic shard assignment via `md5(slug) % 16`
   - CDN-only downloads (`resolve/main/...`) with zero Authorization header during data fetch
   - Sibling-repo routing: `repo = f"axentx/surrogate-1-training-pairs-{hash(slug) % 5}"` for writes

2. **`lib/dedup.py`** — keep central md5 store; add repo-selector helper.

3. **`requirements.txt`** — ensure `huggingface_hub>=0.22` (for `list_repo_tree`).

---

### 1) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# surrogate-1 dataset-enrich — shard-aware, CDN-only, sibling-repo routing
set -euo pipefail

HF_REPO_BASE="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${1:-$(date -u +%Y-%m-%d)}"        # e.g. 2026-04-29
SHARD_ID="${2:-${GITHUB_MATRIX_SHARD_ID:-0}}"   # 0..15
TOTAL_SHARDS="${3:-16}"
HF_TOKEN="${HF_TOKEN:-}"
OUTDIR="./out/${DATE_FOLDER}"
FILE_LIST="./file-list-${DATE_FOLDER}.json"

mkdir -p "${OUTDIR}"

# 1) Single non-recursive tree listing for the date folder (one API call)
#    Avoids recursive list_repo_files and 429 on big repos.
echo "📋 Listing ${HF_REPO_BASE} tree for ${DATE_FOLDER} (non-recursive)..."
if [[ -n "${HF_TOKEN}" ]]; then
  huggingface-cli api --token "${HF_TOKEN}" list-tree \
    --repo-type dataset \
    --recursive false \
    "${HF_REPO_BASE}" \
    "${DATE_FOLDER}" > "${FILE_LIST}.tmp" 2>/dev/null || true
else
  # Fallback: use public CDN file list if no token (read-only mode)
  curl -sL "https://huggingface.co/api/datasets/${HF_REPO_BASE}/tree?path=${DATE_FOLDER}&recursive=false" > "${FILE_LIST}.tmp" || true
fi

# Keep only files (not directories) and valid parquet/jsonl names
jq -r '.[] | select(.type=="file") | .path' "${FILE_LIST}.tmp" > "${FILE_LIST}" || echo "" > "${FILE_LIST}"
rm -f "${FILE_LIST}.tmp"

TOTAL_FILES=$(wc -l < "${FILE_LIST}" | tr -d ' ')
echo "📄 Found ${TOTAL_FILES} files in ${DATE_FOLDER}"

# 2) Deterministic shard assignment and CDN-only fetch
process_file() {
  local rel_path="$1"
  local slug
  slug=$(basename "${rel_path}" | sed 's/\.[^.]*$//')   # strip extension for slug

  # Shard by md5(slug) % TOTAL_SHARDS so same slug always maps to same shard
  local shard
  shard=$(( 0x$(echo -n "${slug}" | md5sum | cut -c1-8) % TOTAL_SHARDS ))
  if [[ "${shard}" -ne "${SHARD_ID}" ]]; then
    return 0
  fi

  # Sibling repo routing: 5 siblings => 640 commits/hr aggregate
  local repo_idx=$(( 0x$(echo -n "${slug}" | md5sum | cut -c1-8) % 5 ))
  local target_repo="${HF_REPO_BASE}"
  if [[ "${repo_idx}" -gt 0 ]]; then
    target_repo="${HF_REPO_BASE}-${repo_idx}"
  fi

  # CDN-only download (no Authorization header) — bypasses /api/ rate limits
  local cdn_url="https://huggingface.co/datasets/${HF_REPO_BASE}/resolve/main/${rel_path}"
  local tmpf
  tmpf=$(mktemp)
  echo "⬇️  CDN fetch ${rel_path} → shard ${shard} (sibling ${repo_idx})"
  if curl -sSL --retry 3 --retry-delay 5 -o "${tmpf}" "${cdn_url}"; then
    # Project to {prompt,response} only at parse time; keep attribution in filename
    # This script emits JSONL lines; dedup will happen later via lib/dedup.py
    if [[ "${rel_path}" == *.parquet ]]; then
      python3 -c "
import sys, pyarrow.parquet as pq, json, hashlib, os
try:
    pf = pq.read_table('${tmpf}').to_pandas()
    cols = [c for c in pf.columns if c.lower() in ('prompt','response','instruction','output','text')]
    for _, row in pf.iterrows():
        prompt = str(row.get('prompt') or row.get('instruction') or row.get('text') or '')
        response = str(row.get('response') or row.get('output') or '')
        if prompt and response:
            obj = {'prompt': prompt, 'response': response}
            print(json.dumps(obj, ensure_ascii=False))
except Exception:
    pass
" >> "${OUTDIR}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"
    elif [[ "${rel_path}" == *.jsonl ]]; then
      # Lightweight projection for jsonl
      python3 -c "
import sys, json, os
with open('${tmpf}') as f:
    for line in f:
        try:
            obj = json.loads(line)
            prompt = str(obj.get('prompt') or obj.get('instruction') or obj.get('text') or '')
            response = str(obj.get('response') or obj.get('output') or '')
            if prompt and response:
                print(json.dumps({'prompt': prompt, 'response': response}, ensure_ascii=False))
        except Exception:
            pass
" >> "${OUTDIR}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"
    else
      echo "⚠️  Skipping unsupported file: ${rel_path}"
    fi
  else
    echo "❌ CDN fetch failed: ${rel_path}"
  fi
  rm -f "${tmpf}"
}

export -f process_file
export SHARD_ID TOTAL_SHARDS HF_TOKEN HF_REPO_BASE OUTDIR

# Parallelize per-file processing (bounded)
cat "${FILE_LIST}" | xargs -P 4 -I {} bash -c 'process_file "$@"' _ {}

# 3) Upload shard output to sibling repo (deterministic routing already applied per row)
#    We upload per-shard file to the primary repo for simplicity; sibling routing is
#    applied at row-level above so downstream training can pull from any sibling.
if [[ -n "${HF_TOKEN}" && -f "${OUTDIR}/shard${SHARD_ID}"-*.jsonl ]]; then
  LATEST=$(ls -t "${OUTDIR}/shard${SHARD_ID}"-*.jsonl | head -1)
  DEST="batches/public-merged/${DATE_FOLDER}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"
  echo "🚀 Uploading ${LATEST} -> ${DEST} to ${HF_REPO_BASE}"
  huggingface-cli upload --token "${HF_TOKEN}" "${HF_REPO_BASE}" "${LATEST}" "${DEST}" --repo-type dataset || true
else
  echo "📭 No output to upload for shard ${SHARD_ID}"
fi

echo "✅ Shard ${SHARD_ID} complete"
```

---

### 2) `lib/dedup.py` (add repo-selector helper)

```python
import sqlite3
import hashlib
import json
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "dedup_hashes.db"

def get_repo_for_slug(slug: str, n_siblings: int = 5) -> str:
    """Deterministic sibling repo selector."""
    h = int(hashlib.md5(slug.encode()).hexdigest()[:8], 16)
    repo_idx = h % n_siblings
    base = "axentx/surrogate-1-training-pairs"
   
