# surrogate-1 / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

### Diagnosis (merged, de-duplicated)
- **No CDN bypass**: workers use `load_dataset(streaming=True)` which triggers HF API auth/429; public files can be fetched via CDN without auth.
- **No pre-listed manifest**: every shard re-enumerates repo files on each run, wasting quota and risking 429.
- **No deterministic shard mapping**: shards stream the entire dataset and discard most rows; redundant download and compute.
- **Schema heterogeneity**: mixed schemas in repo risk `pyarrow.CastError` with `load_dataset(streaming=True)`.
- **No reuse of running compute**: matrix spawns fresh runners each tick even when prior shard work is still valid.
- **Missing pre-flight file list**: no cached `file-list.json` produced after rate-limit window clears, forcing all shards to list/resolve independently.

---

### Proposed Change (merged)
- Add a **Mac-side orchestration script** (`bin/list-and-schedule.sh`) that:
  1. Calls `list_repo_tree(recursive=False)` once per date folder (today + yesterday) via `huggingface_hub`.
  2. Persists `file-list-{date}.json` into the repo (committed or artifact).
  3. Embeds that list into the GitHub Actions matrix payload so each shard only processes its deterministic slice.
- Modify `bin/dataset-enrich.sh` to:
  1. Accept a newline-delimited file list on stdin (or via env file).
  2. Use `hf_hub_download` per file (CDN URL) instead of `load_dataset(streaming=True)`.
  3. Project to `{prompt,response}` at parse time; ignore extra columns.
  4. Handle JSONL, JSON, and Parquet robustly; skip malformed rows.
- Update `.github/workflows/ingest.yml` to:
  1. Accept an optional `file_list_artifact` input.
  2. Download the artifact and feed shard-specific lines to each runner.
  3. Skip matrix shards whose slice is empty.
  4. Cache dedup DB and prior outputs to reuse running compute where valid.

---

### Implementation (merged, corrected, complete)

#### `bin/list-and-schedule.sh`
```bash
#!/usr/bin/env bash
# Mac-side orchestrator: run after HF API rate-limit window clears.
# Produces file-list-{date}.json and optional workflow_dispatch payload.
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="file-lists"
mkdir -p "$OUT_DIR"

# Requires: pip install huggingface_hub
python3 - "$REPO" "$OUT_DIR" <<'PY'
import json, os, sys
from datetime import datetime, timedelta
from huggingface_hub import HfApi

repo = sys.argv[1]
out_dir = sys.argv[2]
api = HfApi()

def date_folders():
    today = datetime.utcnow().date()
    for i in range(2):
        d = today - timedelta(days=i)
        yield d.strftime("%Y-%m-%d")

for folder in date_folders():
    try:
        # non-recursive to avoid pagination explosion
        nodes = api.list_repo_tree(repo=repo, path=folder, recursive=False)
        files = [n.rfilename for n in nodes if not n.rfilename.endswith("/")]
    except Exception as e:
        print(f"Skipping {folder}: {e}", file=sys.stderr)
        continue
    out_path = os.path.join(out_dir, f"file-list-{folder}.json")
    with open(out_path, "w") as f:
        json.dump({"date": folder, "files": files}, f)
    print(f"Wrote {len(files)} files -> {out_path}")
PY

# Optional: create workflow_dispatch matrix payload
cat > payload.json <<EOF
{
  "ref": "main",
  "inputs": {
    "file_list_artifact": "$(ls -1 "$OUT_DIR"/file-list-*.json | head -1)"
  }
}
EOF
echo "To trigger: gh workflow run ingest.yml --json '$(<payload.json)'"
```

#### `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# Updated worker: CDN-only ingestion, per-file download, schema projection.
set -euo pipefail

HF_REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="batches/public-merged/${DATE}"
mkdir -p "$OUT_DIR"

# Dedup store (central)
DEDUP_DB="/tmp/dedup.db"
python3 -c "import lib.dedup; lib.dedup.init_db('$DEDUP_DB')" 2>/dev/null || {
  python3 -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)')
conn.commit()
conn.close()
" "$DEDUP_DB"
}

# Read file list from stdin (one path per line)
mapfile -t FILES
TOTAL_FILES="${#FILES[@]}"
if [[ "$TOTAL_FILES" -eq 0 ]]; then
  echo "No files assigned to shard $SHARD_ID. Exiting."
  exit 0
fi

# Deterministic slice
PER_SHARD=$(( (TOTAL_FILES + TOTAL_SHARDS - 1) / TOTAL_SHARDS ))
START=$(( SHARD_ID * PER_SHARD ))
END=$(( START + PER_SHARD ))
if [[ "$START" -ge "$TOTAL_FILES" ]]; then
  echo "Shard $SHARD_ID out of range. Exiting."
  exit 0
fi
SLICE_FILES=( "${FILES[@]:$START:$PER_SHARD}" )

TIMESTAMP=$(date -u +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

python3 - "$HF_REPO" "$DEDUP_DB" "${SLICE_FILES[@]}" <<'PY' > "$OUT_FILE"
import json, hashlib, os, sys, sqlite3
from pathlib import Path
from huggingface_hub import hf_hub_download

HF_REPO = sys.argv[1]
DEDUP_DB = sys.argv[2]
FILES = sys.argv[3:]

conn = sqlite3.connect(DEDUP_DB)
conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")

def is_dup(md5):
    cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
    return cur.fetchone() is not None

def mark_dup(md5):
    conn.execute("INSERT OR IGNORE INTO seen (md5) VALUES (?)", (md5,))
    conn.commit()

def normalize_record(obj):
    if not isinstance(obj, dict):
        return None
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
    if isinstance(prompt, str) and isinstance(response, str):
        p = prompt.strip()
        r = response.strip()
        if p or r:
            return {"prompt": p, "response": r}
    return None

def process_jsonl(path):
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        rec = normalize_record(obj)
        if not rec:
            continue
        md5 = hashlib.md5(json.dumps(rec, sort_keys=True).encode()).hexdigest()
        if is_dup(md5):
            continue
        mark_dup(md5)
        yield json.dumps(rec, ensure_ascii=False)

def process_json(path):
    try:
        data = json.loads(path.read_text())
    except Exception:
        return
    items = data if isinstance(data, list) else [data]
    for obj in items:
        rec = normalize_record(obj)
        if not rec:
            continue
        md5 = hashlib.md5(json.dumps(rec, sort_keys=True).encode()).hexdigest()
        if is_dup(md5):
            continue
        mark_dup(md5)
        yield json.dumps(rec, ensure_ascii=False)

def process_parquet(path):
    try:
        import pandas as pd

