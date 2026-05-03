# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit pressure and recursive `list_repo_files` by switching to:

1. **Single non-recursive `list_repo_tree(path, recursive=False)` per date folder** (one API call per folder).
2. **Deterministic shard-to-sibling routing** (hash slug → 1 of 5 repos) to stay under 128 commits/hr/repo.
3. **CDN-only fetches** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero Authorization header during training/ingest.
4. **Pre-list once, embed file list JSON** in training script so Lightning Studio does CDN-only data loads (zero API calls while training).

---

### Files to change
- `bin/dataset-enrich.sh` — main worker script (updated)
- `lib/repo_router.py` — new deterministic router
- `lib/dedup.py` — keep dedup logic unchanged
- (optional) `file-list.json` generation for training scripts

---

### 1) Deterministic repo router (`lib/repo_router.py`)

```python
# lib/repo_router.py
import hashlib

SIBLING_REPOS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-training-pairs-sib1",
    "axentx/surrogate-1-training-pairs-sib2",
    "axentx/surrogate-1-training-pairs-sib3",
    "axentx/surrogate-1-training-pairs-sib4",
]

def repo_for_slug(slug: str) -> str:
    """Deterministic repo assignment from slug."""
    digest = hashlib.md5(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]
```

---

### 2) Optional orchestrator helper (`bin/list-date-folder.sh`)

Run once per date folder (e.g., on Mac or orchestrator) to produce `file-list-<date>.json`.

```bash
#!/usr/bin/env bash
# bin/list-date-folder.sh
# Usage: HF_TOKEN=... ./bin/list-date-folder.sh <date-folder> > file-list-<date>.json
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
FOLDER="${1:-}"
if [[ -z "$FOLDER" ]]; then
  echo "Usage: $0 <date-folder>" >&2
  exit 1
fi

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

api = HfApi(token=os.environ.get("HF_TOKEN"))
repo = os.environ.get("REPO", "$REPO")
folder = os.environ.get("FOLDER", "$FOLDER")

tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
files = [item.rfilename for item in tree if item.type == "file"]
sys.stdout.write(json.dumps(files))
PY
```

---

### 3) Updated worker script (`bin/dataset-enrich.sh`)

Key changes:
- Use `list_repo_tree` per date folder (non-recursive) instead of recursive `list_repo_files`.
- Build CDN URLs for downloads (no auth header).
- Keep per-shard deterministic routing for uploads.
- Emit a `file-list.json` for training scripts (optional but recommended).

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: non-recursive tree + CDN-only fetches + repo routing
set -euo pipefail

# -- config --
HF_REPO="datasets/axentx/surrogate-1-training-pairs"
HF_API="https://huggingface.co/api"
HF_CDN="https://huggingface.co/datasets"
DATE=$(date +%Y-%m-%d)
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
HF_TOKEN=${HF_TOKEN:-""}
OUTDIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"
FILE_LIST="file-list-${DATE}.json"

mkdir -p "$(dirname "${OUTFILE}")"

# -- helpers --
hf_api() {
  curl -sSf -H "Authorization: Bearer ${HF_TOKEN}" "$@"
}

# non-recursive tree for a date folder
list_date_files() {
  local folder="$1"
  hf_api "${HF_API}/models/${HF_REPO}/tree?path=${folder}&recursive=false" \
    | jq -r '.tree[] | select(.type=="file") | .path'
}

# deterministic repo for upload
repo_for_slug() {
  python3 -c "import sys; from lib.repo_router import repo_for_slug; print(repo_for_slug(sys.argv[1]))" "$1"
}

# cdn download (no auth)
cdn_download() {
  local repo="$1"
  local path="$2"
  local out="$3"
  curl -sSfL "${HF_CDN}/${repo}/resolve/main/${path}" -o "${out}"
}

# -- main --
echo "[$(date)] Shard ${SHARD_ID}/${TOTAL_SHARDS} starting for ${DATE}"

# 1) list files once (single API call per folder)
mapfile -t ALL_FILES < <(list_date_files "${DATE}" || true)
if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "[$(date)] No files for ${DATE}. Exiting."
  exit 0
fi

# optional: emit file list for training scripts
printf '%s\n' "${ALL_FILES[@]}" | jq -R -s -c 'split("\n")[:-1]' > "${FILE_LIST}"
echo "[$(date)] Listed ${#ALL_FILES[@]} files -> ${FILE_LIST}"

# 2) deterministic shard assignment
SHARDED_FILES=()
for f in "${ALL_FILES[@]}"; do
  # simple hash-based shard assignment (stable across runs)
  HASH=$(echo -n "$f" | md5sum | awk '{print $1}')
  VAL=$(( 0x${HASH:0:8} ))
  if (( VAL % TOTAL_SHARDS == SHARD_ID )); then
    SHARDED_FILES+=("$f")
  fi
done

echo "[$(date)] Shard ${SHARD_ID} processing ${#SHARDED_FILES[@]} files"

# 3) stream, normalize, dedup, upload
processed=0
for path in "${SHARDED_FILES[@]}"; do
  slug=$(basename "${path}" .jsonl || basename "${path}" .parquet || basename "${path}")
  tmp=$(mktemp)

  # CDN fetch (bypasses API auth/rate limits)
  cdn_download "axentx/surrogate-1-training-pairs" "${path}" "${tmp}" || {
    echo "[$(date)] WARN: CDN fetch failed for ${path}, skipping"
    rm -f "${tmp}"
    continue
  }

  # normalize/project to {prompt,response} here (schema-specific logic)
  # Example: if parquet -> use python helper; if jsonl -> jq
  # Keep this lightweight; delegate heavy schema logic to python helpers if needed.
  python3 - <<PY
import json, sys, os, pyarrow.parquet as pq, pyarrow as pa
tmp = "${tmp}"
out = "${tmp}.processed"
try:
    if tmp.endswith(".parquet"):
        tbl = pq.read_table(tmp, columns=["prompt", "response"])
        with open(out, "w") as f:
            for b in tbl.to_batches():
                df = b.to_pydict()
                for i in range(len(df["prompt"])):
                    f.write(json.dumps({"prompt": df["prompt"][i], "response": df["response"][i]}) + "\n")
    else:
        # assume jsonl with prompt/response or adapt per schema
        with open(tmp) as fin, open(out, "w") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                fout.write(json.dumps({"prompt": obj.get("prompt", ""), "response": obj.get("response", "")}) + "\n")
except Exception as e:
    sys.stderr.write(f"normalize error {tmp}: {e}\\n")

