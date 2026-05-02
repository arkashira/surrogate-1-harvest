# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_tree` in `bin/dataset-enrich.sh` with a **deterministic pre-flight snapshot + CDN-only fetches**. This eliminates HF API rate limits (429), prevents schema-cast errors from heterogeneous files, and reduces per-shard memory pressure.

### Steps (1h 30m total)

1. **Add pre-flight snapshot script** (`bin/list-snapshot.sh`) — runs on Mac/orchestrator before workflow dispatch. Uses a single `list_repo_tree` call per date folder, saves `snapshot.json` with `{path, size, sha, url}`. Embeds snapshot into workflow via artifact or repo file. (20m)
2. **Update `bin/dataset-enrich.sh`** — accept snapshot file path or inline JSON; iterate entries and fetch each file via CDN URL (`https://huggingface.co/datasets/.../resolve/main/...`). Parse only `{prompt,response}` projection; stream-append to shard output. Remove `datasets.load_dataset` usage. (40m)
3. **Update `.github/workflows/ingest.yml`** — add `SHARD_ID`/`SHARD_TOTAL` matrix (already present). Add step to fetch snapshot artifact (or read from repo). Pass snapshot path to `dataset-enrich.sh`. Keep 16-shard parallelism. (20m)
4. **Update `lib/dedup.py`** — no schema changes; keep md5 store behavior. Ensure it works with streamed JSONL lines. (10m)
5. **Test locally** — dry-run with small snapshot subset; verify memory <1GB/shard and no HF API calls during fetch. (20m)

---

### Code Snippets

#### 1) `bin/list-snapshot.sh` (or Python equivalent)

```bash
#!/usr/bin/env bash
# Usage: ./bin/list-snapshot.sh <date> > snapshot-<date>.json
# Requires: gh (or huggingface_hub) authenticated with HF_TOKEN for list_repo_tree
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="snapshot-${DATE}.json"

# Use huggingface_hub via python for reliable tree listing (single call per folder)
python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo_id = sys.argv[1]
date = sys.argv[2]
out_path = sys.argv[3]

api = HfApi()
# Non-recursive listing for the date folder
entries = api.list_repo_tree(repo_id=repo_id, path=date, recursive=False)

snapshot = []
for e in entries:
    if e.type != "file":
        continue
    snapshot.append({
        "path": f"{date}/{e.path.split('/')[-1]}",
        "size": getattr(e, "size", 0),
        "sha": getattr(e, "sha", ""),
        "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{date}/{e.path.split('/')[-1]}"
    })

with open(out_path, "w") as f:
    json.dump(snapshot, f, indent=2)
print(f"Wrote {len(snapshot)} entries to {out_path}", file=sys.stderr)
PY
```

Make executable:
```bash
chmod +x bin/list-snapshot.sh
```

---

#### 2) Updated `bin/dataset-enrich.sh` (core worker)

```bash
#!/usr/bin/env bash
# Usage: SHARD_ID=0 SHARD_TOTAL=16 ./bin/dataset-enrich.sh snapshot.json
# Streams files listed in snapshot.json, fetches via CDN, projects {prompt,response},
# dedups via lib/dedup.py, writes shard output.
set -euo pipefail

HF_REPO="datasets/axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
SNAPSHOT_PATH="${1:-snapshot.json}"
OUT_DIR="batches/public-merged/$(date +%Y-%m-%d)"
mkdir -p "$OUT_DIR"
TS="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# Deterministic shard assignment by filename hash
shard_for() {
  local slug="$1"
  # deterministic 0..SHARD_TOTAL-1
  python3 -c "import hashlib; print(int(hashlib.md5('$slug'.encode()).hexdigest(), 16) % $SHARD_TOTAL)"
}

# Project only {prompt,response} from common formats (parquet/jsonl/csv)
project_record() {
  local file="$1"
  local format="${file##*.}"
  python3 "$TMP_DIR/project.py" "$file"
}

# Create projection helper
cat > "$TMP_DIR/project.py" <<'PY'
import sys, json, pyarrow.parquet as pq, pyarrow as pa, csv, io, os

def project_pyarrow(table):
    cols = set(table.column_names)
    # Try common surrogate-1 conventions
    prompt_col = next((c for c in ("prompt", "input", "question", "instruction") if c in cols), None)
    response_col = next((c for c in ("response", "output", "answer", "completion") if c in cols), None)
    if prompt_col and response_col:
        return table.select([prompt_col, response_col]).rename_columns(["prompt", "response"])
    # Fallback: pick first text-like column pair
    texty = [c for c in cols if table.schema.field(c).type in (pa.string(), pa.large_string())]
    if len(texty) >= 2:
        return table.select(texty[:2]).rename_columns(["prompt", "response"])
    return None

def main():
    path = sys.argv[1]
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".parquet":
            tbl = pq.read_table(path, columns=None)
            proj = project_pyarrow(tbl)
            if proj is None:
                return
            for batch in proj.to_batches(max_chunksize=1000):
                df = batch.to_pydict()
                for i in range(len(df["prompt"])):
                    print(json.dumps({"prompt": df["prompt"][i], "response": df["response"][i]}))
        elif ext == ".jsonl":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or obj.get("instruction")
                    response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion")
                    if prompt is not None and response is not None:
                        print(json.dumps({"prompt": prompt, "response": response}))
        elif ext == ".csv":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    prompt = row.get("prompt") or row.get("input") or row.get("question") or row.get("instruction")
                    response = row.get("response") or row.get("output") or row.get("answer") or row.get("completion")
                    if prompt is not None and response is not None:
                        print(json.dumps({"prompt": prompt, "response": response}))
        else:
            # skip unknown
            pass
    except Exception:
        # skip malformed files
        pass

if __name__ == "__main__":
    main()
PY

# Load snapshot
SNAPSHOT="$(cat "$SNAPSHOT_PATH")"
TOTAL="$(echo "$SNAPSHOT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")"
echo "Processing $TOTAL files (shard $SHARD_ID/$SHARD_TOTAL) -> $OUT_FILE" >&2

processed=0
skipped=0
echo "$SNAPSHOT" | python3 -c "
import sys, json, subprocess, os, tempfile, hashlib

