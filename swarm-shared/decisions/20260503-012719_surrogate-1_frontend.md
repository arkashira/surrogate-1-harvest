# surrogate-1 / frontend

## Final Implementation (merged + reconciled)

**Core idea**: eliminate recursive HF API calls and per-file auth during ingestion by:
1. Running **one non-recursive `list_repo_tree` per date folder** (bootstrap from Mac orchestrator) and saving a deterministic file list.
2. Workers ingest **via CDN (`resolve/main/...`)** with no Authorization header.
3. **Deterministic shard-to-repo routing** (`hash(slug) % N`) spreads HF commit load across sibling repos to stay under per-repo commit caps.
4. Keep existing shard assignment, streaming, schema projection, and dedup behavior unchanged.

---

## 1) Bootstrap helper (run from Mac orchestrator) — `bin/list-and-save.sh`

Produces a non-recursive file list for a date folder. Run once per date when rate-limit window clears.

```bash
#!/usr/bin/env bash
# Usage: HF_TOKEN=... ./bin/list-and-save.sh <date-folder> [output.json]
# Example: ./bin/list-and-save.sh 2026-05-03 file-list-2026-05-03.json
set -euo pipefail

REPO="${HF_REPO_BASE:-datasets/axentx/surrogate-1-training-pairs}"
DATE_PATH="${1:-}"
OUT="${2:-file-list.json}"

if [[ -z "$DATE_PATH" ]]; then
  echo "Usage: $0 <date-folder> [output.json]"
  exit 1
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN required for list_repo_tree (one-time bootstrap)"
  exit 1
fi

python3 - "$REPO" "$DATE_PATH" "$OUT" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo_id, date_path = sys.argv[1], sys.argv[2]
out_path = sys.argv[3]

api = HfApi(token=os.environ["HF_TOKEN"])
# Non-recursive: list immediate children in the date folder
tree = api.list_repo_tree(repo_id=repo_id, path=date_path, recursive=False)

files = []
for item in tree:
    # item.path is like "2026-05-03/somefile.parquet"
    if not item.path.endswith("/"):  # skip subfolders
        files.append(item.path)

with open(out_path, "w") as f:
    json.dump({"date": date_path, "files": sorted(files)}, f, indent=2)

print(f"Saved {len(files)} files to {out_path}")
PY
```

Make executable:
```bash
chmod +x bin/list-and-save.sh
```

---

## 2) Updated worker — `bin/dataset-enrich.sh`

Key changes:
- Accept `FILE_LIST_JSON` (or default `file-list.json`) containing the non-recursive list.
- Fetch via CDN (`resolve/main/...`) without auth.
- Deterministic repo routing for commits: `repo = sibling_repos[hash(slug) % N]`.
- Keep streaming + schema projection + dedup behavior.

```bash
#!/usr/bin/env bash
# Surrogate-1 shard worker (GitHub Actions + local)
# Usage:
#   SHARD_ID=0 SHARD_TOTAL=16 FILE_LIST_JSON=file-list.json ./bin/dataset-enrich.sh
#
# Behavior:
# - Reads FILE_LIST_JSON (non-recursive file list for a date folder)
# - Streams each file via CDN (no auth) to avoid HF API rate limits
# - Projects to {prompt,response} per schema
# - Dedups via central md5 store (lib/dedup.py)
# - Writes to sibling repo determined by slug hash to spread commit cap
#
set -euo pipefail

# ---- Configuration ----
HF_REPO_BASE="${HF_REPO_BASE:-datasets/axentx/surrogate-1-training-pairs}"
# Sibling repos for spreading commit cap (HF commit cap 128/hr/repo)
SIBLING_REPOS=(
  "datasets/axentx/surrogate-1-training-pairs"
  "datasets/axentx/surrogate-1-shard-a"
  "datasets/axentx/surrogate-1-shard-b"
  "datasets/axentx/surrogate-1-shard-c"
  "datasets/axentx/surrogate-1-shard-d"
)

SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
FILE_LIST_JSON="${FILE_LIST_JSON:-file-list.json}"
HF_TOKEN="${HF_TOKEN:-}"
DATE_STR="$(date -u +%Y%m%d)"
TIME_STR="$(date -u +%H%M%S)"
OUT_DIR="batches/public-merged/$(date -u +%Y-%m-%d)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIME_STR}.jsonl"

mkdir -p "$(dirname "$OUT_FILE")"

if [[ -z "$HF_TOKEN" ]]; then
  echo "WARNING: HF_TOKEN not set — will not be able to push results."
fi

# ---- Helpers ----
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard:$SHARD_ID] $*"; }

# Deterministic repo selection by slug
select_repo_by_slug() {
  local slug="$1"
  local hash
  hash=$(echo -n "$slug" | cksum | awk '{print $1}')
  local idx=$(( hash % ${#SIBLING_REPOS[@]} ))
  echo "${SIBLING_REPOS[$idx]}"
}

# ---- Validate file list ----
if [[ ! -f "$FILE_LIST_JSON" ]]; then
  log "FILE_LIST_JSON not found: $FILE_LIST_JSON"
  log "Run bin/list-and-save.sh <date-folder> to produce it (one-time bootstrap)."
  exit 1
fi

FILE_LIST="$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for p in data['files']:
    print(p)
" "$FILE_LIST_JSON")"

if [[ -z "$FILE_LIST" ]]; then
  log "No files in $FILE_LIST_JSON"
  exit 0
fi

TOTAL_FILES=$(echo "$FILE_LIST" | wc -l | tr -d ' ')
log "Found $TOTAL_FILES files in $FILE_LIST_JSON"

# ---- Shard assignment ----
# Deterministic sharding by file path to ensure same file always maps to same shard
assign_shard() {
  local path="$1"
  local hash
  hash=$(echo -n "$path" | cksum | awk '{print $1}')
  echo $(( hash % SHARD_TOTAL ))
}

# ---- Process ----
PROCESSED=0
SAVED=0

while IFS= read -r rel_path; do
  [[ -z "$rel_path" ]] && continue

  file_shard=$(assign_shard "$rel_path")
  if [[ "$file_shard" != "$SHARD_ID" ]]; then
    continue
  fi

  # CDN fetch (bypasses HF API auth/rate-limit)
  CDN_URL="https://huggingface.co/datasets/${HF_REPO_BASE}/resolve/main/${rel_path}"
  log "Fetching ($((++PROCESSED))/$TOTAL_FILES shard-assigned): $rel_path"

  # Use python to stream + project to {prompt,response} + dedup
  python3 - "$CDN_URL" "$rel_path" "$OUT_FILE" <<'PY'
import sys, json, hashlib, os, tempfile, shutil, gzip, io
from pathlib import Path

CDN_URL = sys.argv[1]
REL_PATH = sys.argv[2]
OUT_FILE = sys.argv[3]

# Import dedup helper (must be importable)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
try:
    from dedup import is_duplicate, ensure_store
    DEDUP_AVAILABLE = True
except Exception:
    DEDUP_AVAILABLE = False
    _seen = set()

def is_duplicate_fallback(digest):
    if digest in _seen:
        return True
    _seen.add(digest)
    return False

def stream_cdn(url):
    import requests
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        # Transparent gzip handling if needed
        if r.headers.get("content-encoding") == "gzip":
            buf
