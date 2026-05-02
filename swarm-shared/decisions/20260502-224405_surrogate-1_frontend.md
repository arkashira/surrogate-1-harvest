# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single-call tree lister that saves `file-list.json` for a date folder (avoids recursive `list_repo_files`).  
2. **`bin/dataset-enrich.sh`** — read `file-list.json`, stream each file via CDN URL (`resolve/main/...`) with `curl`/`wget`, parse, project to `{prompt,response}`, emit shard JSONL.  
3. **`.github/workflows/ingest.yml`** — add step to generate `file-list.json` once (runs on `ubuntu-latest`), then pass to the 16 shard matrix jobs as artifact.

---

### 1. `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out file-list.json

Notes:
- Uses list_repo_tree(path, recursive=False) per folder to avoid 429.
- CDN downloads (resolve/main) are NOT counted against API rate limits.
- Output is deterministic (sorted paths) so shards are reproducible.
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi, list_repo_tree

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset files for a date folder.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN environment variable required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)
    root_path = f"batches/public-merged/{args.date}"

    # Single non-recursive tree call for the date folder
    entries = list_repo_tree(
        repo_id=args.repo,
        path=root_path,
        repo_type="dataset",
        recursive=False,
    )

    files = sorted(
        e.rfilename
        for e in entries
        if e.type == "file" and e.rfilename.lower().endswith((".jsonl", ".parquet", ".json"))
    )

    payload = {
        "repo": args.repo,
        "date": args.date,
        "root_path": root_path,
        "files": files,
        "count": len(files),
    }

    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 2. `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic shard worker: reads file-list.json, streams via CDN,
# projects to {prompt,response}, dedups via lib/dedup.py, emits shard JSONL.
#
# Required env:
#   HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
#   SHARD_ID          - 0..15
#   SHARD_TOTAL       - 16
#   DATE_FOLDER       - e.g. 2026-05-02
#   FILE_LIST         - path to file-list.json (default: file-list.json)
#
# Output:
#   batches/public-merged/<DATE>/shard<SHARD_ID>-<TS>.jsonl

set -euo pipefail
SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${DATE_FOLDER:-}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
FILE_LIST="${FILE_LIST:-file-list.json}"
TS="$(date -u +%H%M%S)"
OUT_DIR="batches/public-merged/${DATE_FOLDER}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

if [[ -z "${DATE_FOLDER}" ]]; then
  echo "ERROR: DATE_FOLDER is required" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"

# Dedup helper (central md5 store)
DEDUPE_PY="lib/dedup.py"
if [[ ! -f "${DEDUPE_PY}" ]]; then
  echo "ERROR: ${DEDUPE_PY} not found" >&2
  exit 1
fi

# Read file list
mapfile -t FILES < <(python3 -c "
import json, sys
with open('${FILE_LIST}') as f:
    data = json.load(f)
for p in data['files']:
    print(p)
")

TOTAL_FILES="${#FILES[@]}"
if [[ "${TOTAL_FILES}" -eq 0 ]]; then
  echo "No files found for ${DATE_FOLDER}"
  exit 0
fi

# Deterministic shard assignment by slug-hash
shard_for() {
  local slug="$1"
  # deterministic 0..15
  python3 -c "import hashlib; print(int(hashlib.md5('${slug}'.encode()).hexdigest(), 16) % ${SHARD_TOTAL})"
}

process_file() {
  local rel_path="$1"
  local cdn_url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"

  # Stream via CDN (no Authorization header -> bypasses /api/ rate limits)
  # Supports .jsonl and .parquet (via python fallback)
  local ext="${rel_path##*.}"
  ext="$(echo "${ext}" | tr '[:upper:]' '[:lower:]')"

  if [[ "${ext}" == "jsonl" ]]; then
    curl -fsSL --retry 3 --retry-delay 5 "${cdn_url}"
  elif [[ "${ext}" == "parquet" ]]; then
    python3 -c "
import pyarrow.parquet as pq
import sys
try:
    table = pq.read_table('${rel_path}', columns=['prompt','response'])
    for i in range(table.num_rows):
        row = table.slice(i,1).to_pydict()
        print('{\"prompt\":' + json.dumps(row['prompt'][0]) + ',\"response\":' + json.dumps(row['response'][0]) + '}')
except Exception as e:
    sys.stderr.write(str(e) + '\n')
" 2>/dev/null || true
  else
    # fallback: try to parse as json lines via python
    python3 -c "
import json, sys
try:
    with open('${rel_path}') as f:
        for line in f:
            line=line.strip()
            if line: print(line)
except:
    pass
" 2>/dev/null || true
  fi
}

echo "Shard ${SHARD_ID}/${SHARD_TOTAL} processing ${TOTAL_FILES} files from ${DATE_FOLDER}"

count=0
emitted=0
for rel_path in "${FILES[@]}"; do
  count=$((count + 1))
  slug="$(basename "${rel_path}" | sed 's/\.[^.]*$//')"
  s="$(shard_for "${slug}")"
  if [[ "${s}" -ne "${SHARD_ID}" ]]; then
    continue
  fi

  process_file "${rel_path}" | while IFS= read -r line; do
    line="$(echo "${line}" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "${line}" ]] && continue

    # Project to {prompt,response} only (schema normalization)
    prompt="$(echo "${line}" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(json.dumps(d.get('prompt','')))" 2>/dev/null || echo '""')"
    response="$(echo "${line}" | python3 -c
