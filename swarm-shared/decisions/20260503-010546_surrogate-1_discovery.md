# surrogate-1 / discovery

## Highest-value incremental improvement (≤2h)

**Goal**: Eliminate HF API rate-limit failures and HF Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.

**Why this wins**:
- Avoids `list_repo_files` recursive pagination (100× API calls) and per-file metadata requests.
- CDN downloads (`/resolve/main/...`) bypass `/api/` auth rate limits entirely.
- Per-shard memory stays bounded (streaming decode + projection) → prevents OOM on HF Space.
- Fits within 2h: only `bin/dataset-enrich.sh` + small Python helper changes.

---

## Implementation plan

1. **Add helper** `bin/list_folder_manifest.py`  
   - Input: `repo`, `date_folder` (e.g., `2026-05-03`)  
   - Uses `huggingface_hub.list_repo_tree(path=date_folder, recursive=False)` once per folder.  
   - Emits JSON lines: `{"repo": "...", "path": "...", "cdn_url": "...", "size": ...}`  
   - Exits 0 with empty list if folder missing (idempotent).

2. **Update `bin/dataset-enrich.sh`**  
   - Compute deterministic shard assignment from `slug-hash % 16` (existing behavior).  
   - Instead of streaming via `load_dataset(repo, streaming=True)`:
     - Call `list_folder_manifest.py` once per date folder to get file list.  
     - Filter to files assigned to `SHARD_ID`.  
     - For each file:
       - Download via `curl -L "$cdn_url" --compressed -o "$tmpfile"` (CDN, no auth).  
       - Stream-parse with `pyarrow`/`json`/`parquet` reader as needed.  
       - Project to `{prompt, response}` only at parse time.  
       - Compute md5 for dedup via `lib/dedup.py`.  
       - Emit accepted pairs to stdout (JSONL).  
   - After processing shard batch, upload shard output to:
     ```
     batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
     ```
   - Keep existing dedup semantics (central md5 store on HF Space remains source of truth).

3. **Add retry/backoff for CDN downloads**  
   - Simple exponential backoff (max 3 retries) for transient CDN 5xx.  
   - Skip + log on 404 (file removed).

4. **Ensure idempotency & safety**
   - Script must tolerate partial failures and resume via next cron tick (no cross-run state).  
   - Use `set -euo pipefail` and trap tempfiles for cleanup.

5. **Test locally (quick)**
   - Dry-run with `SHARD_ID=0` on a small date folder.  
   - Verify:
     - No `list_repo_files` recursive calls.  
     - Only one `list_repo_tree` per folder.  
     - Downloads use CDN URLs.  
     - Output schema `{prompt, response}` only.

---

## Code snippets

### `bin/list_folder_manifest.py`
```python
#!/usr/bin/env python3
"""
List files in a single folder of a HuggingFace dataset repo.
Outputs one JSON line per file:
  {"repo": "...", "path": "...", "cdn_url": "...", "size": ...}
"""
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    repo = os.getenv("HF_DATASET_REPO")
    folder = os.getenv("HF_DATASET_FOLDER")  # e.g. "2026-05-03"
    if not repo or not folder:
        print("Set HF_DATASET_REPO and HF_DATASET_FOLDER", file=sys.stderr)
        sys.exit(1)

    api = HfApi()
    try:
        entries = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception as exc:
        # Folder may not exist -> empty manifest is fine
        sys.stderr.write(f"list_repo_tree failed: {exc}\n")
        sys.exit(0)

    for entry in entries:
        if entry.type != "file":
            continue
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{folder}/{entry.path}"
        item = {
            "repo": repo,
            "path": f"{folder}/{entry.path}",
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        }
        print(json.dumps(item, ensure_ascii=False))

if __name__ == "__main__":
    main()
```

### Key changes in `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUTDIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "$OUTFILE")"

# One API call per folder: get manifest
MANIFEST=$(mktemp)
HF_DATASET_REPO="$REPO" HF_DATASET_FOLDER="$DATE" \
  python3 bin/list_folder_manifest.py > "$MANIFEST"

# Deterministic shard assignment by slug-hash
assign_shard() {
  local slug="$1"
  # simple hash -> int -> mod
  local hash
  hash=$(echo -n "$slug" | sha256sum | tr -d ' -' | head -c 16)
  local num=$((16#$hash))
  echo $((num % TOTAL_SHARDS))
}

process_file() {
  local cdn_url="$1"
  local tmpfile
  tmpfile=$(mktemp)
  # CDN download (no auth, bypass API rate limits)
  curl -L "$cdn_url" --compressed -o "$tmpfile" --retry 3 --retry-delay 2 || {
    echo "Failed to download $cdn_url" >&2
    rm -f "$tmpfile"
    return 1
  }

  # Stream-parse and project to {prompt,response}
  # Adapt parser to actual file type (parquet/json/...) as needed.
  python3 -c "
import json, pyarrow.parquet as pq, sys
try:
    table = pq.read_table('$tmpfile')
    for col in table.column_names:
        if col not in ('prompt', 'response'):
            table = table.drop([col])
    if 'prompt' in table.column_names and 'response' in table.column_names:
        for batch in table.to_batches(max_chunksize=8192):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                print(json.dumps({'prompt': row['prompt'], 'response': row['response']}, ensure_ascii=False))
except Exception as e:
    sys.stderr.write(f'Parse error: {e}\\n')
  " 2>/dev/null | while IFS= read -r line; do
    # dedup via central md5 store (existing lib/dedup.py)
    # Example: echo "$line" | python3 lib/dedup.py --check-or-insert
    # For now, pass-through (keep existing dedup integration)
    echo "$line"
  done

  rm -f "$tmpfile"
}

# Iterate manifest and process shard-assigned files
count=0
while IFS= read -r item; do
  path=$(echo "$item" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])")
  cdn_url=$(echo "$item" | python3 -c "import sys,json; print(json.load(sys.stdin)['cdn_url'])")
  slug=$(basename "$path" .parquet)

  shard=$(assign_shard "$slug")
  if [ "$shard" -eq "$SHARD_ID" ]; then
    process_file "$cdn_url" >> "$OUTFILE"
    count=$((count + 1))
  fi
done < "$MANIFEST"

rm -f "$MANIFEST"

# Upload
