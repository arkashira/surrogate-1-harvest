# surrogate-1 / frontend

## Implementation Plan (≤2h) — Highest-value frontend-adjacent fix

**Goal**: Eliminate HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline by replacing recursive authenticated fetches with a single `list_repo_tree` + CDN-only fetches. This is the core fix from the design/backend notes and maps directly to the “HF CDN Bypass” and “pre-list file paths once” patterns.

### Scope (frontend-aligned)
- Update `bin/dataset-enrich.sh` to:
  1. Accept a date folder (or default to latest) and produce a deterministic shard slice from a single `list_repo_tree` call.
  2. Download only assigned files via CDN (`resolve/main/...`) with no Authorization header during data fetch.
  3. Keep existing HF_TOKEN usage only for repo metadata + final push.
- Keep `lib/dedup.py` unchanged (it’s the central md5 store).
- Keep output format unchanged (`batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`).

### Why this is highest value
- Removes 429 risk during data streaming (CDN tier has much higher limits).
- Cuts per-shard authenticated API calls from O(files) to O(1).
- Fits <2h: single script change + small Python helper for tree parsing.

---

## Concrete changes

### 1) Add lightweight tree lister (Python helper)

Create `bin/list_tree.py`:

```python
#!/usr/bin/env python3
"""
Single non-recursive tree lister for a date folder.
Usage:
  python3 bin/list_tree.py <repo> <date_folder> > filelist.json
"""
import json
import os
import sys
from huggingface_hub import HfApi

def main():
    if len(sys.argv) != 3:
        print("Usage: list_tree.py <repo> <date_folder>", file=sys.argv)
        sys.exit(1)
    repo = sys.argv[1]
    folder = sys.argv[2].rstrip("/")
    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Non-recursive: one API call per date folder
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    entries = []
    for item in tree:
        if item.type == "file":
            entries.append(item.path)
    # Also include immediate subfolders' files in one extra call per subfolder
    # (cheap because recursive=False per subfolder)
    subfolders = [item.path for item in tree if item.type == "folder"]
    for sub in subfolders:
        try:
            sub_tree = api.list_repo_tree(repo=repo, path=sub, recursive=False)
            for item in sub_tree:
                if item.type == "file":
                    entries.append(item.path)
        except Exception:
            continue
    json.dump(entries, sys.stdout)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_tree.py
```

---

### 2) Update `bin/dataset-enrich.sh`

Key changes:
- Accept `DATE_FOLDER` and `SHARD_ID`/`SHARD_TOTAL` (from matrix).
- Run `list_tree.py` once, shard paths deterministically by hash of path.
- Download via CDN (no auth header) and parse into `{prompt,response}`.
- Keep dedup via `lib/dedup.py`.
- Output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

```bash
#!/usr/bin/env bash
set -euo pipefail

# surrogate-1 dataset-enrich.sh
# Updated: single tree list + CDN-only fetches

REPO="${HF_DATASET_REPO:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
HF_TOKEN="${HF_TOKEN:-}"

OUT_DIR="batches/public-merged/${DATE_FOLDER}"
TS=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "${OUT_DIR}"

echo "[$(date)] Shard ${SHARD_ID}/${SHARD_TOTAL} | Date folder: ${DATE_FOLDER}"

# 1) List files once (non-recursive per folder)
echo "[$(date)] Listing tree for ${DATE_FOLDER}..."
mapfile -t ALL_PATHS < <(python3 bin/list_tree.py "${REPO}" "${DATE_FOLDER}" | python3 -c "import sys,json; print('\n'.join(json.load(sys.stdin)))")

if [ ${#ALL_PATHS[@]} -eq 0 ]; then
  echo "[$(date)] No files found. Exiting."
  exit 0
fi

# 2) Deterministic shard assignment by path hash
mapfile -t MY_PATHS < <(
  for p in "${ALL_PATHS[@]}"; do
    # deterministic hash bucket
    h=$(echo -n "$p" | sha256sum | cut -c1-8)
    bucket=$(( 0x$h % SHARD_TOTAL ))
    if [ "$bucket" -eq "$SHARD_ID" ]; then
      echo "$p"
    fi
  done
)

echo "[$(date)] Shard ${SHARD_ID} assigned ${#MY_PATHS[@]} files"

# 3) Download via CDN (no auth header) and process
# Helper to extract prompt/response from a file (parquet or jsonl)
process_file() {
  local url="$1"
  local tmpf
  tmpf=$(mktemp)
  # CDN fetch — no Authorization header
  curl -fsSL --retry 3 --retry-delay 2 -o "${tmpf}" "${url}" || { rm -f "${tmpf}"; return 1; }

  # Try parquet -> jsonl projection (prompt,response)
  if python3 -c "import pyarrow.parquet as pq; pq.read_table('${tmpf}')" 2>/dev/null; then
    python3 -c "
import pyarrow.parquet as pq, json, sys
tbl = pq.read_table('${tmpf}')
for col in ['prompt','response']:
  if col not in tbl.column_names:
    # try common aliases
    pass
# project only what we need
proj = tbl.select(['prompt','response']) if 'prompt' in tbl.column_names and 'response' in tbl.column_names else tbl
for b in proj.to_batches():
    for i in range(b.num_rows):
        row = {k: b.column(k)[i].as_py() for k in b.schema.names}
        # normalize to string
        prompt = str(row.get('prompt','')).strip()
        response = str(row.get('response','')).strip()
        if prompt and response:
            print(json.dumps({'prompt':prompt,'response':response}, ensure_ascii=False))
" 2>/dev/null | while IFS= read -r line; do
      echo "$line"
    done
  else
    # Assume JSONL-like; extract prompt/response
    python3 -c "
import json, sys
for line in open('${tmpf}', 'r', encoding='utf-8', errors='ignore'):
    line=line.strip()
    if not line: continue
    try:
        obj=json.loads(line)
    except:
        continue
    prompt=str(obj.get('prompt', obj.get('input', obj.get('question', '')))).strip()
    response=str(obj.get('response', obj.get('output', obj.get('answer', '')))).strip()
    if prompt and response:
        print(json.dumps({'prompt':prompt,'response':response}, ensure_ascii=False))
" 2>/dev/null | while IFS= read -r line; do
      echo "$line"
    done
  fi
  rm -f "${tmpf}"
}

# Dedup helper (central store on HF Space) — unchanged usage
DEDUP_STORE="lib/dedup.py"

TOTAL=${#MY_PATHS[@]}
COUNT=0
for rel_path in "${MY_PATHS[@]}"; do
  COUNT=$((COUNT+1))
  CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  echo "[$(date)] (${COUNT}/${TOTAL}) Fetching ${rel_path} via CDN..."

  # Stream process file and dedup
  process_file "${CDN_URL}" | while IFS= read -r line; do
    # Compute md5
