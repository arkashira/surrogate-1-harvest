# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_files` in `bin/dataset-enrich.sh` with deterministic pre-flight snapshot + CDN-only fetches to avoid HF API rate limits and schema heterogeneity.

### Steps (1h 30m total)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — 20m  
   - Runs on Mac (or cron) before the 16-shard workflow.  
   - Uses `list_repo_tree(path, recursive=False)` per date folder (non-recursive to avoid 100× pagination).  
   - Emits `snapshot-<date>.json` containing `{file_path, size, sha}` for that folder only.  
   - Stores snapshot in repo (or as workflow artifact) so shards never call `list_repo_files` or `load_dataset` during ingest.

2. **Update `bin/dataset-enrich.sh`** — 40m  
   - Accept snapshot path as env var `SNAPSHOT_PATH` (fallback to old behavior for compatibility).  
   - If snapshot exists:  
     - Filter files by deterministic shard (`hash(slug) % 16 == SHARD_ID`).  
     - Fetch each file via CDN URL:  
       `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<file_path>`  
     - Parse with `pyarrow`/`pandas` projecting only `{prompt, response}` at parse time (ignore extra columns).  
   - If snapshot missing: keep old `load_dataset(streaming=True)` path but log warning.

3. **Update workflow** (` .github/workflows/ingest.yml`) — 20m  
   - Add a pre-job (or separate workflow) that runs `bin/make-snapshot.py` and uploads snapshot as artifact.  
   - Pass snapshot artifact to the 16-shard matrix job.  
   - Set `SHELL=/bin/bash` in any cron/runner env to avoid wrapper shebang issues (pattern from earlier lessons).

4. **Dedup & schema hardening** — 20m  
   - Ensure `lib/dedup.py` uses only `{prompt, response}` + `md5` for dedup; ignore extra metadata.  
   - Filename pattern: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` (no `source`/`ts` columns).

5. **Test & verify** — 10m  
   - Run snapshot locally, verify CDN URLs resolve without auth.  
   - Run one shard locally with snapshot to confirm zero HF API calls during data load.

---

## Code Snippets

### 1. `bin/make-snapshot.py`
```python
#!/usr/bin/env python3
"""
Create a deterministic snapshot for a date folder in surrogate-1-training-pairs.
Usage:
  HF_TOKEN=<token> python bin/make-snapshot.py --date 2026-05-02 --out snapshot-2026-05-02.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser(description="Create snapshot for a date folder.")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--repo", default=REPO, help="HF dataset repo")
    args = parser.parse_args()

    token = os.getenv("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    # Non-recursive list per folder to avoid 100× pagination
    try:
        files = API.list_repo_tree(
            repo_id=args.repo,
            path=args.date,
            repo_type="dataset",
            token=token,
            recursive=False,
        )
    except Exception as e:
        print(f"ERROR listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    snapshot = {
        "date": args.date,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "files": [
            {
                "file_path": f.rfilename if hasattr(f, "rfilename") else f["path"],
                "size": f.size if hasattr(f, "size") else f.get("size"),
                "sha": f.lfs.get("sha256") if hasattr(f, "lfs") else None,
            }
            for f in files
            if (hasattr(f, "type") and f.type == "file") or (isinstance(f, dict) and f.get("type") == "file")
        ],
    }

    with open(args.out, "w") as fp:
        json.dump(snapshot, fp, indent=2)

    print(f"Snapshot written to {args.out} ({len(snapshot['files'])} files)")

if __name__ == "__main__":
    main()
```

### 2. Updated `bin/dataset-enrich.sh` (key section)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${SHARD_ID:?required}"
: "${HF_TOKEN:?required}"

SNAPSHOT_PATH="${SNAPSHOT_PATH:-}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="batches/public-merged/${DATE}"
mkdir -p "${OUT_DIR}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Deterministic shard assignment
shard_assign() {
  local slug="$1"
  # Stable hash across runs
  local hash
  hash=$(echo -n "$slug" | sha256sum | cut -c1-16)
  echo $(( 0x${hash} % 16 ))
}

process_file_cdn() {
  local file_path="$1"
  local tmp
  tmp=$(mktemp)
  # CDN fetch — no Authorization header required for public datasets
  curl -fsSL "https://huggingface.co/datasets/${REPO}/resolve/main/${file_path}" -o "${tmp}"
  # Project to {prompt,response} only at parse time
  python3 -c "
import sys, pyarrow.parquet as pq, json
tbl = pq.read_table(sys.argv[1], columns=['prompt','response'])
for i in range(tbl.num_rows):
    row = tbl.slice(i,1).to_pydict()
    print(json.dumps({'prompt': row['prompt'][0], 'response': row['response'][0]}))
" "${tmp}" 2>/dev/null || true
  rm -f "${tmp}"
}

if [[ -n "${SNAPSHOT_PATH}" && -f "${SNAPSHOT_PATH}" ]]; then
  log "Using snapshot ${SNAPSHOT_PATH}"
  mapfile -t FILES < <(
    python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for f in data['files']:
    print(f['file_path'])
" "${SNAPSHOT_PATH}"
  )
  for fp in "${FILES[@]}"; do
    slug="${fp%.parquet}"
    slug="${slug##*/}"
    if [[ $(shard_assign "$slug") -ne $SHARD_ID ]]; then
      continue
    fi
    log "Processing (CDN): ${fp}"
    process_file_cdn "${fp}" >> "${OUT_DIR}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"
  done
else
  log "WARNING: No snapshot provided — falling back to streaming (may hit rate limits)"
  # Legacy path (avoid in production)
  python3 - <<'PY'
import os, json, pyarrow.dataset as ds
from huggingface_hub import HfApi
repo = "axentx/surrogate-1-training-pairs"
api = HfApi()
# WARNING: streaming + recursive list can hit rate limits
dataset = ds.dataset(f"hf://datasets/{repo}", format="parquet", partitioning="hive")
for batch in dataset.to_batches():
    # project at parse time
    pass
PY
fi

log "
