# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal:** Eliminate HF API rate-limit risk and per-shard recursive listing in `bin/dataset-enrich.sh` by replacing runtime `load_dataset(streaming=True)` + `list_repo_files` with deterministic pre-flight snapshots and CDN-only fetches.

---

### 1. Snapshot Generator (`bin/make-snapshot.sh`)
Run once per date folder on Mac (or CI) before shards start.

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: make-snapshot.sh <date-folder> [output.json]
# Example: make-snapshot.sh public-merged/2026-05-02 snapshot/2026-05-02.json

REPO="axentx/surrogate-1-training-pairs"
FOLDER="${1:-public-merged/$(date +%Y-%m-%d)}"
OUT="${2:-snapshot/$(basename "$FOLDER").json}"

mkdir -p "$(dirname "$OUT")"

python3 - "$REPO" "$FOLDER" "$OUT" <<'PY'
import json, os, sys
from datetime import datetime
from huggingface_hub import HfApi

repo_id, folder, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()

tree = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
files = sorted(item.rfilename for item in tree if item.type == "file")

if not files:
    sys.exit(f"No files found in {repo_id}/{folder}")

snapshot = {
    "repo": repo_id,
    "folder": folder,
    "date": os.path.basename(folder.rstrip("/")),
    "files": files,
    "created_at": datetime.utcnow().isoformat() + "Z"
}

with open(out_path, "w") as f:
    json.dump(snapshot, f, indent=2)
print(f"Snapshot written to {out_path} ({len(files)} files)")
PY
```

---

### 2. Updated Worker (`bin/dataset-enrich.sh`)
Deterministic shard assignment + CDN downloads + schema-safe projection.

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-snapshot/$(date +%Y-%m-%d).json}"

# Resolve file list: snapshot (preferred) or legacy fallback
if [[ -f "$SNAPSHOT_FILE" ]]; then
  echo "Using snapshot: $SNAPSHOT_FILE"
  mapfile -t ALL_FILES < <(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    for p in json.load(f)['files']:
        print(p)
" "$SNAPSHOT_FILE")
else
  echo "WARNING: No snapshot at $SNAPSHOT_FILE — falling back to list_repo_tree (rate-limited)"
  mapfile -t ALL_FILES < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
folder = 'public-merged/$(date +%Y-%m-%d)'
items = api.list_repo_tree(repo_id='$REPO', path=folder, recursive=False)
for i in items:
    if i.type == 'file':
        print(i.rfilename)
")
fi

# Deterministic shard assignment by filename hash
shard_files=()
for f in "${ALL_FILES[@]}"; do
  hash=$(python3 -c "import hashlib, sys; print(int(hashlib.md5(sys.argv[1].encode()).hexdigest(), 16))" "$f")
  if (( hash % TOTAL_SHARDS == SHARD_ID )); then
    shard_files+=("$f")
  fi
done

echo "Shard $SHARD_ID processing ${#shard_files[@]} files"

# CDN download with retry/backoff and schema-safe projection
process_file() {
  local rel_path="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  local tmpfile
  tmpfile=$(mktemp)

  for attempt in 1 2 3; do
    if curl -fsSL --retry 3 --retry-delay 2 --retry-max-time 15 "$url" -o "$tmpfile"; then
      break
    fi
    echo "Retry $attempt for $url"
    sleep $(( RANDOM % 5 + attempt ))
  done

  python3 - "$tmpfile" "$rel_path" <<'PY'
import pyarrow.parquet as pq
import sys, json, os

tmpfile, rel_path = sys.argv[1], sys.argv[2]
try:
    table = pq.read_table(tmpfile, columns=["prompt", "response"])
except Exception:
    try:
        table = pq.read_table(tmpfile)
        if "prompt" not in table.column_names or "response" not in table.column_names:
            raise ValueError("Missing prompt/response columns")
        table = table.select(["prompt", "response"])
    except Exception as e:
        print(f"Skipping {rel_path}: {e}", file=sys.stderr)
        os.unlink(tmpfile)
        sys.exit(0)

df = table.to_pandas()
for _, row in df.iterrows():
    print(json.dumps({"prompt": str(row["prompt"]), "response": str(row["response"])}, ensure_ascii=False))

os.unlink(tmpfile)
PY
}

export -f process_file
export REPO

# Parallelize per-file processing (lightweight), keep ordering non-critical
printf '%s\n' "${shard_files[@]}" | xargs -P 4 -I {} bash -c 'process_file "$@"' _ {} \
  | gzip > "output/shard-${SHARD_ID}-$(date +%H%M%S).jsonl.gz"

# Upload to dataset repo (preserve existing behavior)
DATE=$(date +%Y-%m-%d)
TS=$(date +%H%M%S)
DEST="batches/public-merged/${DATE}/shard-${SHARD_ID}-${TS}.jsonl"

echo "Uploading to ${REPO}:${DEST}"
gzip -d < "output/shard-${SHARD_ID}-${TS}.jsonl.gz" | \
  python3 -c "
import sys
from huggingface_hub import upload_file
upload_file(
    path_or_fileobj=sys.stdin.buffer,
    path_in_repo='$DEST',
    repo_id='$REPO',
    repo_type='dataset',
    commit_message='shard $SHARD_ID'
)"
```

---

### 3. GitHub Actions (`ingest.yml`) — Minimal Change
Pass snapshot consistently across shards.

```yaml
# Add before matrix (or commit snapshot to repo for simplicity)
# Option A: generate once and upload as artifact for all shards
# Option B: commit snapshot/YYYY-MM-DD.json to repo (simplest, deterministic)

jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install deps
        run: pip install huggingface_hub pyarrow pandas
      - name: Use snapshot (if present)
        env:
          SNAPSHOT_FILE: snapshot/${{ github.run_date }}.json
        run: |
          if [[ -f "$SNAPSHOT_FILE" ]]; then
            echo "SNAPSHOT_FILE=$SNAPSHOT_FILE" >> $GITHUB_ENV
          fi
      - name: Run shard
        env:
          SHARD_ID: ${{ matrix.shard }}
          TOTAL_SHARDS: 16
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: bash bin/dataset-enrich.sh
```

---

### 4. Mac Helper (`tools/preflight.sh`)
Convenience for local/dev runs.

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
LATEST=$(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree(repo_id
