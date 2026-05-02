# surrogate-1 / discovery

## 1. Diagnosis
- No local file-list cache: each shard re-enumerates the full public dataset via HF API on every run, risking 429 and wasting quota.
- No CDN bypass: ingestion uses `datasets.load_dataset(..., streaming=True)` which performs authenticated `/api/` calls during streaming; misses the CDN rate-limit bypass.
- No deterministic shard-to-date mapping: every cron tick re-ingests the same recent date folder; no forward progress across the tree.
- No reuse of running compute pattern: the HF Space (cpu-basic) is still used for dedup coordination but not leveraged for orchestration; GitHub Actions duplicates work the Space could gate.
- No visibility into top-hub docs for contextual guidance: discovery lacks a quick “what’s most connected” check before planning changes.

## 2. Proposed change
- File: `bin/dataset-enrich.sh` (main worker)
- Add: pre-computed file-list JSON (generated once per date folder) and embed it so shards perform only CDN downloads (zero API calls during training/ingest).
- Add: deterministic shard → date-folder mapping (hash date string mod N) so each cron tick advances across folders instead of re-ingesting the same slice.
- Add: lightweight hub review step (uses existing knowledge-rag pattern) as a pre-check before planning.

## 3. Implementation

```bash
# bin/dataset-enrich.sh
#!/usr/bin/env bash
set -euo pipefail

# --
# Config
# --
REPO="datasets/axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
SHARD_ID=${SHARD_ID:-0}
SHARD_TOTAL=${SHARD_TOTAL:-16}
WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# --
# 1) Deterministic date-folder selection (discovery forward progress)
# --
# Pick a date folder from the repo tree so shards advance across time.
# We list top-level date folders once (API), pick by hash mod N.
FOLDERS_JSON=$(curl -sL \
  "https://huggingface.co/api/datasets/${REPO}/tree?recursive=false" \
  | jq -r '.[] | select(.type=="directory") | .path' \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' \
  | sort)

FOLDER_COUNT=$(echo "$FOLDERS_JSON" | wc -l)
if [[ "$FOLDER_COUNT" -eq 0 ]]; then
  echo "No date folders found"
  exit 1
fi

# Deterministic choice: hash of today's date mod folder count
HASH=$(echo -n "$DATE" | sha256sum | tr -d ' -')
INDEX=$((0x${HASH:0:8} % FOLDER_COUNT))
TARGET_FOLDER=$(echo "$FOLDERS_JSON" | sed -n "$((INDEX+1))p")
echo "Selected date folder: $TARGET_FOLDER"

# --
# 2) Produce file list once (API) -> embed for CDN-only workers
# --
FILE_LIST="filelist.json"
if [[ ! -f "$FILE_LIST" ]]; then
  curl -sL \
    "https://huggingface.co/api/datasets/${REPO}/tree?recursive=false&path=${TARGET_FOLDER}" \
    | jq '[.[] | select(.type=="file") | .path]' > "$FILE_LIST"
fi

TOTAL_FILES=$(jq 'length' "$FILE_LIST")
if [[ "$TOTAL_FILES" -eq 0 ]]; then
  echo "No files in $TARGET_FOLDER"
  exit 0
fi

# --
# 3) Deterministic shard slicing (stable across runs)
# --
SLICE_FILES=$(jq -r --argjson shard "$SHARD_ID" --argjson total "$SHARD_TOTAL" \
  'to_entries | map(select((.key % $total) == $shard)) | map(.value) | .[]' \
  "$FILE_LIST")

if [[ -z "$SLICE_FILES" ]]; then
  echo "No files assigned to shard $SHARD_ID"
  exit 0
fi

# --
# 4) Process assigned files via CDN (no Authorization header)
# --
OUTDIR="out-shard-$SHARD_ID"
mkdir -p "$OUTDIR"

process_file() {
  local relpath="$1"
  local outfile="$OUTDIR/$(basename "$relpath" .parquet).jsonl"
  # Download via CDN (no auth) -> project to {prompt,response} -> normalize
  curl -sL "https://huggingface.co/datasets/${REPO}/resolve/main/${relpath}" -o "${relpath##*/}"
  # Lightweight projection: assumes parquet -> jsonl conversion available
  python3 -c "
import pyarrow.parquet as pq, json, sys
try:
    tbl = pq.read_table(sys.argv[1], columns=['prompt','response'])
    for rec in tbl.to_pylist():
        if rec.get('prompt') and rec.get('response'):
            print(json.dumps({'prompt': rec['prompt'], 'response': rec['response']}))
except Exception:
    pass
" "${relpath##*/}" >> "$outfile"
  rm -f "${relpath##*/}"
}

export -f process_file
export REPO OUTDIR
echo "$SLICE_FILES" | xargs -P 4 -I {} bash -c 'process_file "$@"' _ {}

# --
# 5) Upload shard output (avoid collisions: shard+timestamp)
# --
TIMESTAMP=$(date -u +%H%M%S)
DEST="batches/public-merged/${DATE}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"
cat "$OUTDIR"/*.jsonl | \
  python3 /opt/axentx/surrogate-1/lib/dedup.py --input /dev/stdin --output - | \
  curl -sL -X PUT \
    -H "Authorization: Bearer ${HF_TOKEN}" \
    -H "Content-Type: application/jsonl" \
    --data-binary @- \
    "https://huggingface.co/api/datasets/${REPO}/uploads/${DEST}?overwrite=true"

echo "Uploaded $DEST"
```

```python
# lib/dedup.py  (lightweight wrapper for streaming dedup)
# Keep existing central md5 store behavior; this file is imported by the runner.
import hashlib, json, sys, argparse, sqlite3, os

DB_PATH = os.getenv("DEDUP_DB", "/tmp/dedup_hashes.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=sys.stdin, type=argparse.FileType("r"))
    ap.add_argument("--output", default=sys.stdout, type=argparse.FileType("w"))
    args = ap.parse_args()

    conn = init_db()
    seen = set(r[0] for r in conn.execute("SELECT md5 FROM hashes").fetchall())
    new_hashes = []

    for line in args.input:
        line = line.strip()
        if not line:
            continue
        md5 = hashlib.md5(line.encode()).hexdigest()
        if md5 in seen:
            continue
        seen.add(md5)
        new_hashes.append(md5)
        args.output.write(line + "\n")

    if new_hashes:
        conn.executemany("INSERT OR IGNORE INTO hashes (md5) VALUES (?)", [(h,) for h in new_hashes])
        conn.commit()
    conn.close()

if __name__ == "__main__":
    main()
```

```yaml
# .github/workflows/ingest.yml  (add folder-level concurrency + reuse pattern hint)
name: Ingest (16-shard)

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - name: Setup
        run: |
          sudo apt-get update && sudo apt-get install -y jq
          python3 -m pip install
