# surrogate-1 / discovery

## Highest-value incremental improvement (≤2h)

**Deterministic date-partitioning + CDN-bypass ingestion with pre-flight file-list**

- Fixes noisy history and training instability by writing to stable, date-partitioned paths (`batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`) so training scripts can pin exact snapshots.
- Eliminates redundant HF API calls and rate-limit pressure by generating a single file-list on the Mac orchestrator and embedding it in the runner (CDN-only fetches during ingestion).
- Prevents overwrite races by including shard+timestamp in filename and using deterministic shard assignment.

---

## Implementation plan (≤2h)

1. Add `bin/list-public-files.py` (run on Mac)  
   - Uses `huggingface_hub.list_repo_tree(..., recursive=False)` per date folder (or root) once.  
   - Emits `file-list-{date}.json` with `{"repo": "...", "paths": [...]}`.  
   - Commits or uploads as artifact for workflow.

2. Update `bin/dataset-enrich.sh`  
   - Accept optional `FILE_LIST` path (default: fallback to current behavior).  
   - If provided, iterate local JSON instead of calling `list_repo_files` in each shard.  
   - Compute deterministic shard: `hash(slug) % 16 == SHARD_ID`.  
   - Stream via CDN URL: `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth header).  
   - Project to `{prompt, response}` at parse time (no schema assumptions).  
   - Write output to `batches/public-merged/{YYYY-MM-DD}/shard{N}-{HHMMSS}.jsonl`.

3. Update `.github/workflows/ingest.yml`  
   - Add step before matrix to generate file-list (or download artifact produced by manual Mac run).  
   - Pass `FILE_LIST` and `PARTITION_DATE` to each matrix job.  
   - Keep 16-shard matrix; ensure `HF_TOKEN` only used for final push (CDN reads are unauthed).

4. Small safety/quality bits  
   - Ensure `lib/dedup.py` uses central store path via env var (unchanged).  
   - Make scripts executable and include `#!/usr/bin/env bash` shebangs.  
   - Add `set -euo pipefail` in bash wrappers.

---

## Code snippets

### bin/list-public-files.py
```python
#!/usr/bin/env python3
"""
Generate a deterministic file list for public dataset ingestion.
Run on Mac orchestrator to avoid per-shard HF API list calls.
Usage:
  python bin/list-public-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out file-list-2026-05-02.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD) or 'all'")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    # If date is a folder under the dataset root
    prefix = "" if args.date == "all" else f"{args.date}/"
    # Use recursive=False to avoid pagination explosion; we'll walk folders if needed
    items = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False, repo_type="dataset")

    paths = []
    for item in items:
        if item.type == "file":
            paths.append(item.path)
        elif item.type == "folder":
            # shallow list inside this folder (one extra hop) to avoid deep recursion
            subitems = api.list_repo_tree(repo_id=args.repo, path=item.path, recursive=False, repo_type="dataset")
            for sub in subitems:
                if sub.type == "file":
                    paths.append(sub.path)

    # Deterministic ordering
    paths.sort()
    payload = {"repo": args.repo, "date": args.date, "paths": paths}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(paths)} paths to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

### bin/dataset-enrich.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

# Deterministic shard ingestion with CDN-bypass.
# Required env:
#   SHARD_ID (0-15)
#   HF_TOKEN (for push only)
#   FILE_LIST (optional): path to JSON file list from list-public-files.py
#   PARTITION_DATE (optional): YYYY-MM-DD; defaults to today

REPO="axentx/surrogate-1-training-pairs"
WORKDIR=$(mktemp -d)
OUTDIR="output"
mkdir -p "$OUTDIR"

PARTITION_DATE="${PARTITION_DATE:-$(date +%Y-%m-%d)}"
TIMESTAMP=$(date +%H%M%S)
OUTPUT_FILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

# Dedup store (central)
DEDUP_DB="${DEDUP_DB:-/tmp/dedup.db}"
export DEDUP_DB

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard:${SHARD_ID}] $*"; }

# Resolve file list
if [[ -n "${FILE_LIST:-}" && -f "$FILE_LIST" ]]; then
  log "Using file list: $FILE_LIST"
  mapfile -t FILE_PATHS < <(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for p in data['paths']:
    print(p)
" "$FILE_LIST")
else
  log "FILE_LIST not provided or missing; falling back to repo listing (slower)."
  mapfile -t FILE_PATHS < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree('$REPO', path='', recursive=True, repo_type='dataset')
for it in items:
    if it.type == 'file':
        print(it.path)
")
fi

TOTAL=${#FILE_PATHS[@]}
log "Processing $TOTAL candidate files"

count=0
skipped=0
deduped=0

for rel_path in "${FILE_PATHS[@]}"; do
  # Deterministic shard assignment by slug (filename without extension)
  slug=$(basename "$rel_path" | sed 's/\.[^.]*$//')
  # Stable hash across runs
  hash_val=$(echo -n "$slug" | md5sum | cut -c1-8)
  shard=$((0x$hash_val % 16))
  if [[ $shard -ne $SHARD_ID ]]; then
    ((skipped++))
    continue
  fi

  # CDN-bypass download (no auth header)
  url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  tmp_file="${WORKDIR}/$(basename "$rel_path")"
  if ! curl -fsSL --retry 3 --retry-delay 5 -o "$tmp_file" "$url"; then
    log "WARN: failed to download $url"
    continue
  fi

  # Project to {prompt,response} at parse time (best-effort)
  # Supports common HF dataset split files and raw jsonl/parquet where possible.
  # For parquet we use python to avoid heavy deps in bash.
  case "$rel_path" in
    *.parquet)
      pairs=$(python3 -c "
import pyarrow.parquet as pq, sys, json, hashlib
try:
    tbl = pq.read_table(sys.argv[1], columns=['prompt','response'])
except Exception:
    try:
        tbl = pq.read_table(sys.argv[1])
    except Exception as e:
        sys.exit(0)
df = tbl.to_pandas()
# Normalize column names
cmap = {c: c.lower().strip() for c in df.columns}
prompt_col = next((c for c in cmap if 'prompt
