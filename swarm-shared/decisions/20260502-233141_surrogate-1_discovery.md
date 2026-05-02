# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_tree` in `bin/dataset-enrich.sh` with a **deterministic pre-flight snapshot + CDN-only fetches**. This eliminates HF API rate limits (429), prevents schema-cast errors from heterogeneous repos, and keeps ingestion within the 128-commit cap by using deterministic shard→repo routing.

### Steps (all executable in <2h)

1. Add `bin/list-snapshot.sh` — run once on Mac (or in workflow before matrix) to produce `file-list.json` for a single date folder using non-recursive tree calls.
2. Update `bin/dataset-enrich.sh` to:
   - Accept `FILE_LIST` (path to JSON) or fallback to current behavior.
   - Use CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for downloads with no Authorization header.
   - Project to `{prompt,response}` only at parse time; drop extra schema columns.
   - Deterministic shard selection: `shard_id = hash(slug) % 16`; skip if not this runner’s `SHARD_ID`.
   - Deterministic repo routing for commits: `repo_index = hash(slug) % 5` → pick from `REPO_TARGETS` (5 sibling repos) to spread writes across 640/hr aggregate.
3. Update `.github/workflows/ingest.yml`:
   - Add an initial job that produces `file-list.json` and uploads it as an artifact.
   - Pass `FILE_LIST` into each matrix shard.
   - Set `SHELL=/bin/bash` and ensure scripts are invoked via `bash`.
4. Add safety: retry on 429 with 360s backoff; idle-stop guard for Lightning reuse (if any Studio calls exist).

---

### 1) `bin/list-snapshot.sh`

```bash
#!/usr/bin/env bash
# Produce a non-recursive snapshot of file paths for a date folder.
# Usage:
#   HF_TOKEN=hf_xxx ./list-snapshot.sh \
#     --repo axentx/surrogate-1-training-pairs \
#     --date 2026-05-02 \
#     --out file-list.json

set -euo pipefail

REPO=""
DATE=""
OUT="file-list.json"

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo) REPO="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --out)  OUT="$2";  shift 2 ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$DATE" ]]; then
  echo "Usage: $0 --repo <owner/repo> --date <YYYY-MM-DD> [--out file.json]"
  exit 1
fi

# Use huggingface_hub Python for reliable tree listing (non-recursive).
python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo_id = sys.argv[1]
date_folder = sys.argv[2]
out_path = sys.argv[3]

api = HfApi(token=os.environ.get("HF_TOKEN"))
# Non-recursive: list top-level entries in the date folder
entries = api.list_repo_tree(repo_id, path=date_folder, recursive=False)

files = []
for e in entries:
    if e.type == "file":
        files.append(f"{date_folder}/{e.path.split('/')[-1]}")

with open(out_path, "w") as f:
    json.dump({"repo": repo_id, "date": date_folder, "files": files}, f)
PY

echo "Snapshot written to $OUT ($(jq '.files | length' "$OUT") files)"
```

Make executable:

```bash
chmod +x bin/list-snapshot.sh
```

---

### 2) `bin/dataset-enrich.sh` (updated)

```bash
#!/usr/bin/env bash
# Worker shard: normalize & upload training pairs.
# Usage (in workflow):
#   SHARD_ID=0 NUM_SHARDS=16 FILE_LIST=file-list.json bash bin/dataset-enrich.sh

set -euo pipefail
export SHELL=/bin/bash

HF_TOKEN="${HF_TOKEN:-}"
REPO_TARGETS=(
  "axentx/surrogate-1-training-pairs"
  "axentx/surrogate-1-shard1"
  "axentx/surrogate-1-shard2"
  "axentx/surrogate-1-shard3"
  "axentx/surrogate-1-shard4"
)

SHARD_ID="${SHARD_ID:-0}"
NUM_SHARDS="${NUM_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"
WORK_DIR="$(mktemp -d)"
OUT_DIR="${WORK_DIR}/out"
mkdir -p "$OUT_DIR"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard $SHARD_ID] $*"; }

# Deterministic helpers
hash_slug() {
  # Stable numeric hash for shard/replica assignment
  echo -n "$1" | sha256sum | tr -d ' -' | head -c 16 | xargs -I{} printf "%d" 0x{}
}

pick_shard() {
  local slug=$1
  local total=$2
  local h=$(hash_slug "$slug")
  echo $(( h % total ))
}

pick_repo() {
  local slug=$1
  local idx=$(( $(hash_slug "$slug") % ${#REPO_TARGETS[@]} ))
  echo "${REPO_TARGETS[$idx]}"
}

# Rate-limit safe retry
retry_360() {
  local n=0 max=3
  until "$@"; do
    code=$?
    n=$((n+1))
    if [[ $n -ge $max ]]; then return $code; fi
    log "Retry $n/$max after 360s backoff for: $*"
    sleep 360
  done
}

# Download via CDN (no auth header) to bypass API rate limits
cdn_download() {
  local repo=$1
  local path=$2
  local out=$3
  local url="https://huggingface.co/datasets/${repo}/resolve/main/${path}"
  curl -fsSL --retry 3 --retry-delay 5 -o "$out" "$url"
}

# Parse file and project to {prompt,response} only
parse_and_project() {
  local file=$1
  local out=$2
  python3 - "$file" "$out" <<'PY'
import json, sys, hashlib, pyarrow as pa, pyarrow.parquet as pq, pyarrow.json as paj

src = sys.argv[1]
dst = sys.argv[2]

try:
    table = pq.read_table(src, columns=["prompt", "response"])
except Exception:
    try:
        table = paj.read_json(src)
        cols = [c for c in table.column_names if c in ("prompt", "response")]
        if not cols:
            # fallback: try to find string columns
            cols = [c for c in table.column_names if table.schema.field(c).type in (pa.string(),)]
            if len(cols) >= 2:
                table = table.select(cols[:2]).rename_columns(["prompt", "response"])
            else:
                raise ValueError("No prompt/response columns")
        else:
            table = table.select(cols)
            if "prompt" not in table.column_names or "response" not in table.column_names:
                table = table.rename_columns(["prompt", "response"] if len(cols)>=2 else ["prompt"] + (["response"] if len(cols)>=2 else []))
    except Exception:
        # last resort: line-delimited json with prompt/response keys
        with open(src, "r") as f:
            rows = []
            for line in f:
                line = line.strip()
                if not line: continue
                obj = json.loads(line)
                rows.append({"prompt": obj.get("prompt", ""), "response": obj.get("response", "")})
        table = pa.Table.from_pylist(rows)

# Ensure string type
table = table.cast(pa.schema([pa.field("prompt", pa.string()), pa.field("response", pa.string())]))
pq.write
