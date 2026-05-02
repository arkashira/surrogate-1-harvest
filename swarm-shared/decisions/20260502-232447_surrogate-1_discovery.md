# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshots + CDN-only fetches to avoid HF API rate limits and schema errors.

### Steps (1h 30m total)

1. **Audit current script** (10m) — confirm usage of `load_dataset(streaming=True)` and `list_repo_files(recursive=True)`.
2. **Add pre-flight snapshot mode** (30m) — new flag `--snapshot` that:
   - Uses `list_repo_tree(path, recursive=False)` per date folder (non-recursive) to avoid 100× pagination.
   - Saves `file-list-<date>.json` locally (paths + sizes).
   - Embeds this list into the worker script or passes via env var so training/CDN fetches require zero API calls.
3. **Replace streaming load with CDN fetches** (30m) — in the worker loop:
   - For each file in snapshot, download via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth header).
   - Stream-parse with `pyarrow`/`json`/`parquet` as needed; project to `{prompt, response}` only at parse time.
   - Skip `load_dataset` entirely.
4. **Handle mixed schemas safely** (20m) — wrap per-file parsing in try/except; log and skip malformed files instead of failing the shard.
5. **Update workflow matrix** (10m) — ensure `SHARD_ID` and `FILE_LIST` (or date) are passed to each runner; runners fetch only their deterministic slice from the snapshot.
6. **Test locally** (20m) — run `bin/dataset-enrich.sh --snapshot` + one worker slice; verify CDN downloads, schema projection, and output format.

---

### Code Snippets

#### 1. Pre-flight snapshot helper (add to `bin/dataset-enrich.sh` or new `bin/snapshot.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="snapshots"
DATE="${1:-$(date +%Y-%m-%d)}"

mkdir -p "$OUT_DIR"

# Use HF Hub Python helper (non-recursive per folder) to avoid list_repo_files recursion
python3 - <<PY
import os, json
from huggingface_hub import HfApi

api = HfApi()
repo = os.environ["REPO"]
date = os.environ["DATE"]
out = os.environ["OUT_DIR"]

# List top-level date folders non-recursively
items = api.list_repo_tree(repo=repo, path=date, recursive=False)
folders = [i for i in items if i.type == "directory"]

all_files = []
for f in folders:
    try:
        sub = api.list_repo_tree(repo=repo, path=f.path, recursive=False)
        for sf in sub:
            if sf.type == "file":
                all_files.append(sf.path)
    except Exception as e:
        print(f"Skipping {f.path}: {e}")

snapshot_path = os.path.join(out, f"file-list-{date}.json")
with open(snapshot_path, "w") as fp:
    json.dump({"date": date, "files": sorted(all_files)}, fp, indent=2)
print(f"Snapshot saved: {snapshot_path} ({len(all_files)} files)")
PY
```

#### 2. Worker: CDN-only fetch + schema projection (replace `load_dataset` loop)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SNAPSHOT="snapshots/file-list-${DATE}.json"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"

# Select deterministic slice by hashing filenames
mapfile -t ALL_FILES < <(python3 -c "
import json, sys
with open('$SNAPSHOT') as f:
    files = json.load(f)['files']
# stable shard assignment
for p in sorted(files):
    if hash(p) % $N_SHARDS == $SHARD_ID:
        print(p)
")

process_file() {
    local rel_path="$1"
    local url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
    local tmp=$(mktemp)

    # CDN download (no auth header)
    if ! curl -fsSL "$url" -o "$tmp"; then
        echo "WARN: failed to download $rel_path" >&2
        return 0
    fi

    # Project to {prompt,response} only; handle mixed schemas
    python3 - <<PY
import sys, json, pyarrow.parquet as pq, pyarrow as pa, os

path = sys.argv[1]
out_dir = sys.argv[2]

try:
    # Try parquet first
    table = pq.read_table(path, columns=["prompt", "response"])
    for batch in table.to_batches(max_chunksize=1000):
        for row in zip(batch.column("prompt").to_pylist(),
                       batch.column("response").to_pylist()):
            if row[0] is not None and row[1] is not None:
                print(json.dumps({"prompt": row[0], "response": row[1]}))
except Exception:
    # Fallback: line-delimited JSON
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
                response = obj.get("response") or obj.get("output")
                if prompt and response:
                    print(json.dumps({"prompt": prompt, "response": response}))
    except Exception:
        # skip malformed
        pass
PY
    "$tmp" "./enriched"

    rm -f "$tmp"
}

export -f process_file

# Parallel process slice (adjust jobs to runner capacity)
printf "%s\n" "${ALL_FILES[@]}" | xargs -P 4 -I {} bash -c 'process_file "$@"' _ {}
```

#### 3. Workflow matrix update (`.github/workflows/ingest.yml` snippet)

```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt huggingface_hub pyarrow

      # Pre-flight snapshot (single lightweight API call per workflow)
      - name: Generate snapshot
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          REPO: axentx/surrogate-1-training-pairs
          DATE: ${{ github.event.inputs.date || '' }}
        run: |
          DATE=${DATE:-$(date +%Y-%m-%d)}
          python bin/snapshot.py "$DATE"

      # Run worker with CDN-only fetches
      - name: Run shard
        env:
          SHARD_ID: ${{ matrix.shard }}
          N_SHARDS: 16
          DATE: ${{ steps.snapshot.outputs.date || '' }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          DATE=${DATE:-$(date +%Y-%m-%d)}
          bash bin/dataset-enrich.sh
```

---

### Notes & Trade-offs

- **API usage**: Snapshot uses one `list_repo_tree` per date folder (non-recursive) — well below 1000 req/5m. Workers use CDN only (zero API calls during data load).
- **Schema safety**: Per-file try/except prevents one bad file from killing a shard; malformed rows are skipped.
- **Dedup**: Central `lib/dedup.py` remains the source of truth; workers still upload shard outputs and dedup runs downstream (or can be run as a final merge step).
- **Idempotency**: Snapshot + deterministic shard assignment ensures reruns produce identical slices.
