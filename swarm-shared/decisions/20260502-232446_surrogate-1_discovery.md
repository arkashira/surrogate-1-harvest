# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshots + CDN-only fetches to avoid HF API rate limits and schema heterogeneity issues.

### Steps (est. 90 min)

1. **Add snapshot utility** (`bin/snapshot.sh`)  
   - Single API call to `list_repo_tree(path, recursive=False)` for today’s folder (or latest date folder)  
   - Save JSON list of `{path, size, sha}` to `snapshot/<date>/files.json`  
   - Commit snapshot to repo (or pass via workflow artifact) so workers never call `list_repo_files`

2. **Rewrite worker entrypoint** (`bin/dataset-enrich.sh`)  
   - Accept snapshot file as argument (default: `snapshot/latest/files.json`)  
   - Deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`  
   - For each assigned file:  
     - Download via CDN URL: `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>` (no auth, no API)  
     - Stream-parse with `pyarrow`/`jsonl` → project to `{prompt, response}` only  
     - Dedup via central md5 store (`lib/dedup.py`)  
     - Append to shard output: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`

3. **Update workflow** (`.github/workflows/ingest.yml`)  
   - Add one lightweight “snapshot” job that runs before matrix, produces `files.json` artifact  
   - Pass artifact to all 16 matrix jobs (or embed snapshot in repo commit for simplicity)  
   - Ensure `SHELL=/bin/bash` and all scripts have `#!/usr/bin/env bash` + `chmod +x`

4. **Safety + observability**  
   - Add retry/backoff for CDN 429 (separate from API 429)  
   - Log per-shard counts and skipped duplicates  
   - Validate shard determinism with quick checksum test

---

## Code Snippets

### 1. Snapshot utility (`bin/snapshot.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="snapshot"
DATE=$(date -u +%Y-%m-%d)
OUT_FILE="${OUT_DIR}/${DATE}/files.json"

mkdir -p "$(dirname "${OUT_FILE}")"

# Single API call: non-recursive tree for today's folder (or root)
# If data is partitioned by date, pass path="public-merged/${DATE}"
python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

api = HfApi()
repo = os.environ["REPO"]
# List root or date folder; adjust path as needed
tree = api.list_repo_tree(repo=repo, path="", recursive=False)
files = [{"path": f.path, "size": f.size, "sha": f.sha} for f in tree if f.type == "file"]
with open(os.environ["OUT_FILE"], "w") as fp:
    json.dump(files, fp, indent=2)
print(f"Snapshot saved: {len(files)} files -> {os.environ['OUT_FILE']}")
PY

# Optional: copy to latest for convenience
mkdir -p "${OUT_DIR}/latest"
cp "${OUT_FILE}" "${OUT_DIR}/latest/files.json"
echo "::set-output name=snapshot::${OUT_FILE}"
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

### 2. Updated worker script (`bin/dataset-enrich.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${HF_TOKEN:?Need HF_TOKEN}"
: "${SHARD_ID:?Need SHARD_ID (0-15)}"
: "${SNAPSHOT_FILE:=snapshot/latest/files.json}"

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%H%M%S)
OUT_DIR="batches/public-merged/${DATE}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "${OUT_DIR}"

# Deterministic shard assignment
in_shard() {
  local slug=$1
  local hash
  hash=$(echo -n "$slug" | sha256sum | cut -c1-8)
  echo $((0x$hash % 16))
}

# Dedup helper (reuse existing lib/dedup.py)
is_duplicate() {
  local md5=$1
  python3 lib/dedup.py --check "$md5"
}

record_pair() {
  local prompt=$1
  local response=$2
  local md5=$3
  python3 lib/dedup.py --add "$md5"
  jq -n --arg p "$prompt" --arg r "$response" '{prompt:$p, response:$r}' >> "${OUT_FILE}"
}

# Process snapshot
jq -r '.[].path' "${SNAPSHOT_FILE}" | while read -r fpath; do
  slug=$(basename "$fpath" | sed 's/\.[^.]*$//')
  if [[ $(in_shard "$slug") -ne $SHARD_ID ]]; then
    continue
  fi

  echo "Processing shard ${SHARD_ID}: ${fpath}"

  # CDN download (no auth, bypasses API rate limits)
  url="https://huggingface.co/datasets/${REPO}/resolve/main/${fpath}"
  tmp=$(mktemp)
  curl -fsSL --retry 3 --retry-delay 5 -o "$tmp" "$url"

  # Parse and project to {prompt,response} only
  # Supports .jsonl and .parquet via simple detection
  case "$fpath" in
    *.parquet)
      python3 - <<PY
import pyarrow.parquet as pq, sys, json, hashlib, os
tmp = sys.argv[1]
try:
    table = pq.read_table(tmp, columns=["prompt", "response"])
except Exception:
    # fallback: read all and project
    table = pq.read_table(tmp)
    if "prompt" not in table.column_names or "response" not in table.column_names:
        # best-effort: use first two text columns
        cols = [c for c in table.column_names if table.schema.types[table.column_names.index(c)].id in (table.schema.types[0].id,)]
        if len(cols) >= 2:
            table = table.select(cols[:2])
            table = table.rename_columns(["prompt", "response"])
        else:
            sys.exit(0)
for batch in table.to_batches():
    for i in range(batch.num_rows):
        prompt = str(batch.column(0)[i].as_py())
        response = str(batch.column(1)[i].as_py())
        md5 = hashlib.md5((prompt + response).encode()).hexdigest()
        print(json.dumps({"prompt": prompt, "response": response, "md5": md5}))
PY
      "$tmp" | while read -r line; do
        prompt=$(echo "$line" | jq -r '.prompt')
        response=$(echo "$line" | jq -r '.response')
        md5=$(echo "$line" | jq -r '.md5')
        if ! is_duplicate "$md5"; then
          record_pair "$prompt" "$response" "$md5"
        fi
      done
      ;;
    *.jsonl|*.json)
      cat "$tmp" | while read -r line; do
        prompt=$(echo "$line" | jq -r '.prompt // empty')
        response=$(echo "$line" | jq -r '.response // empty')
        if [[ -z "$prompt" || -z "$response" ]]; then
          continue
        fi
        md5=$(echo -n "$prompt$response" | md5sum | cut -d' ' -f1)
        if ! is_duplicate "$md5"; then
          record_pair "$prompt" "$response" "$md5"
        fi
      done
      ;;
    *)
      echo "Skipping unsupported file: $fpath"
      ;;
  esac

  rm -f "$tmp"
done

echo "Shard ${SHARD_ID} finished. Output: ${OUT_FILE}"
wc -l "${OUT_FILE}" || true
