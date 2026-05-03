# surrogate-1 / backend

## Highest-value incremental improvement (≤2h)

Replace recursive `list_repo_files` and per-file API calls in `bin/dataset-enrich.sh` with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` only at parse time. This eliminates HF API rate-limit (429) and HF Space OOM while keeping the 16-shard runner architecture intact.

---

## Implementation plan

1. **Update `bin/dataset-enrich.sh`**
   - Replace recursive `list_repo_files` with `list_repo_tree(path, recursive=False)` per date folder.
   - Emit a local `file-list.json` containing CDN paths only.
   - Workers consume `file-list.json` and download via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, no API quota).
   - Keep per-shard deterministic hashing (`slug-hash % 16 == SHARD_ID`) and output naming.

2. **Add lightweight Python helper (`bin/project_cdn.py`)**
   - Stream-download from CDN URL.
   - Parse parquet/JSON per schema, project to `{prompt,response,source_file}`.
   - Output newline JSONL to stdout (so shell can redirect to shard file).

3. **Update `.github/workflows/ingest.yml`**
   - Add one “prepare” job (or first step in each shard) that runs `list_repo_tree` once and uploads `file-list.json` as an artifact.
   - Each shard downloads the artifact and processes its slice.

4. **Keep dedup behavior unchanged**
   - Continue using central `lib/dedup.py` SQLite store for cross-run dedup (best-effort; duplicates across runs are acceptable per trade-offs).

5. **Validate locally**
   - Run `bash bin/dataset-enrich.sh` with `SHARD_ID=0` and `TOTAL_SHARDS=16` on a small date folder.
   - Confirm zero `huggingface_hub` API calls during data fetch (only during initial tree list).

---

## Code snippets

### 1. `bin/dataset-enrich.sh` (updated)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Config
REPO="axentx/surrogate-1-training-pairs"
BASE_DATE="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="batches/public-merged/${BASE_DATE}"
TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "${OUT_FILE}")"

# 1) List files once per date folder via HF API (tree, non-recursive)
#    This is the ONLY API call per run per folder.
echo "Listing tree for ${REPO}/${BASE_DATE}..."
TREE_JSON=$(python3 -c "
import json, os
from huggingface_hub import list_repo_tree
tree = list_repo_tree(repo_id='${REPO}', path='${BASE_DATE}', recursive=False)
# Include nested folders by recursing one level (fast, bounded)
entries = []
for entry in tree:
    if entry.type == 'directory':
        sub = list_repo_tree(repo_id='${REPO}', path=entry.path, recursive=False)
        entries.extend([e for e in sub if e.type == 'file'])
    else:
        entries.append(entry)
print(json.dumps([e.path for e in entries if e.type == 'file']))
")

# Save file list for reproducibility / debugging
echo "${TREE_JSON}" > "file-list-${BASE_DATE}.json"

# 2) Assign files to shards deterministically by slug-hash
mapfile -t ALL_FILES < <(echo "${TREE_JSON}" | python3 -c "
import json, sys, hashlib
files = json.load(sys.stdin)
for f in files:
    print(f)
")

# Filter to this shard
SHARD_FILES=()
for f in "${ALL_FILES[@]}"; do
    HASH=$(echo -n "${f}" | sha256sum | awk '{print $1}')
    BUCKET=$((0x${HASH:0:8} % TOTAL_SHARDS))
    if [ "${BUCKET}" -eq "${SHARD_ID}" ]; then
        SHARD_FILES+=("${f}")
    fi
done

echo "Shard ${SHARD_ID}/${TOTAL_SHARDS} processing ${#SHARD_FILES[@]} files..."

# 3) Process each file via CDN (no auth) and append to shard output
for REL_PATH in "${SHARD_FILES[@]}"; do
    CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${REL_PATH}"
    python3 bin/project_cdn.py "${CDN_URL}" "${REL_PATH}" >> "${OUT_FILE}"
done

echo "Shard output: ${OUT_FILE}"
```

---

### 2. `bin/project_cdn.py` (new)

```python
#!/usr/bin/env python3
"""
Download a single file from HF CDN and project to {prompt,response}.
Usage:
    python3 bin/project_cdn.py <cdn_url> <source_file>
Outputs newline JSONL to stdout:
    {"prompt": "...", "response": "...", "source_file": "..."}
"""

import sys
import json
import pyarrow.parquet as pq
import pyarrow as pa
import tempfile
import requests
from pathlib import Path

CDN_TIMEOUT = 30


def download_cdn(url: str) -> bytes:
    resp = requests.get(url, timeout=CDN_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def project_parquet(content: bytes, source_file: str):
    with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
        tmp.write(content)
        tmp.flush()
        table = pq.read_table(tmp.name)
        # Normalize column names to lowercase
        cols = {c.lower(): c for c in table.column_names}
        # Find prompt/response candidates
        prompt_col = None
        response_col = None
        for c in cols:
            if "prompt" in c:
                prompt_col = cols[c]
            if "response" in c or "completion" in c or "output" in c:
                response_col = cols[c]

        if prompt_col is None or response_col is None:
            # Fallback: try to use first two string columns
            str_cols = [c for c in table.column_names if pa.types.is_string(table.schema.field(c).type)]
            if len(str_cols) >= 2:
                prompt_col, response_col = str_cols[0], str_cols[1]
            else:
                return  # skip

        for batch in table.to_batches():
            df = batch.to_pandas()
            for _, row in df.iterrows():
                yield {
                    "prompt": str(row[prompt_col]).strip(),
                    "response": str(row[response_col]).strip(),
                    "source_file": source_file,
                }


def project_jsonl(content: bytes, source_file: str):
    text = content.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        # Normalize keys
        keys = {k.lower(): k for k in obj.keys()}
        prompt = None
        response = None
        for k in keys:
            if "prompt" in k:
                prompt = obj[keys[k]]
            if "response" in k or "completion" in k or "output" in k:
                response = obj[keys[k]]

        if prompt is None or response is None:
            # Try positional fallback for simple list-of-strings
            if isinstance(obj, list) and len(obj) >= 2:
                prompt, response = str(obj[0]), str(obj[1])
            else:
                continue

        yield {
            "prompt": str(prompt).strip(),
            "response": str(response).strip(),
            "source_file": source_file,
        }


def main():
    if len(sys.argv) < 3:
        print("Usage: project_cdn.py <cdn_url> <source_file>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    source_file = sys.argv[2]

    content = download_cdn(url)
    suffix = Path(source_file).suffix.lower()

    try:

