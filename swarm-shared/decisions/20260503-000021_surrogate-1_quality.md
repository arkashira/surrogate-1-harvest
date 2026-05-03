# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate limits during ingestion and ensures deterministic, reproducible file lists across all 16 shards.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` for a given date folder (e.g., `public-merged/2026-05-02/`).  
   - Outputs `snapshot-<date>.json` containing `{"date":"...","files":["path1","path2",...],"generated_at":"ISO8601"}`.  
   - Validates JSON and exits non-zero on API errors.

2. **Update `bin/dataset-enrich.sh`** (30m)  
   - Accept optional `SNAPSHOT_FILE` env var. If provided, reads file list from snapshot instead of calling `list_repo_files` recursively.  
   - Falls back to current behavior if snapshot missing (for backward compatibility).  
   - Each shard filters files by deterministic hash-slug to maintain 1/16 slicing.

3. **Add `bin/generate-file-list.py`** (25m)  
   - Lightweight Python helper that uses `huggingface_hub` to list tree and filter by prefix/date.  
   - Emits newline-separated paths to stdout and JSON to file.  
   - Handles pagination and 429 retries with 360s backoff.

4. **Update GitHub Actions workflow** (20m)  
   - Add a pre-step job that runs `snapshot.sh` once and uploads artifact `snapshot-<date>.json`.  
   - Pass snapshot path to each matrix shard via `env.SNAPSHOT_FILE`.  
   - Ensure shards download artifact before running.

5. **Update training script integration** (20m)  
   - Add optional flag `--file-list snapshot.json` to training launcher.  
   - When provided, training uses CDN-only downloads (`hf_hub_download` per file) without any `list_repo_*` calls during data loading.  
   - Embed snapshot generation into existing orchestration so Mac only runs snapshot once per date, then Lightning Studio uses CDN-only paths.

6. **Add tests and validation** (10m)  
   - Quick smoke test: run snapshot locally, verify JSON schema, ensure shard filtering produces non-empty sets.  
   - Add `set -euo pipefail` and proper error messages.

---

## Code Snippets

### `bin/generate-file-list.py`
```python
#!/usr/bin/env python3
"""
Generate a snapshot of dataset files for a given date folder.
Usage:
  python generate-file-list.py --repo axentx/surrogate-1-training-pairs \
                              --prefix batches/public-merged/2026-05-02 \
                              --output snapshot-2026-05-02.json
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

from huggingface_hub import HfApi, RepositoryNotFoundError, RateLimitError

def list_files_with_retry(api, repo_id, prefix, max_retries=3):
    for attempt in range(max_retries):
        try:
            # recursive=False to avoid paginating 100x; we'll handle subfolders by prefix
            tree = api.list_repo_tree(repo_id=repo_id, path=prefix, recursive=False)
            files = [item.rfilename for item in tree if item.type == "file"]
            # If prefix is a folder, list_repo_tree with recursive=False returns direct children only.
            # For nested files, rely on prefix filtering by caller.
            return files
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            wait = 360
            print(f"Rate limited. Waiting {wait}s (attempt {attempt+1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
        except RepositoryNotFoundError:
            print(f"Repository {repo_id} not found.", file=sys.stderr)
            raise

def main():
    parser = argparse.ArgumentParser(description="Generate dataset file snapshot.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id")
    parser.add_argument("--prefix", required=True, help="Folder prefix (e.g. batches/public-merged/2026-05-02)")
    parser.add_argument("--output", required=True, help="Output JSON file")
    parser.add_argument("--token", default=None, help="HF token (optional for public reads)")
    args = parser.parse_args()

    api = HfApi(token=args.token)
    try:
        files = list_files_with_retry(api, args.repo, args.prefix)
    except Exception as e:
        print(f"Failed to list files: {e}", file=sys.stderr)
        sys.exit(1)

    snapshot = {
        "repo": args.repo,
        "prefix": args.prefix,
        "date": args.prefix.rstrip("/").split("/")[-1] if "/" in args.prefix else None,
        "files": sorted(files),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    # Also print newline-separated paths to stdout for shell pipelines
    for fpath in snapshot["files"]:
        print(fpath)

if __name__ == "__main__":
    main()
```

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# Generate a snapshot of dataset files for a date folder.
# Usage:
#   SNAPSHOT_DATE=2026-05-02 ./snapshot.sh
# Outputs:
#   snapshot-<date>.json

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
SNAPSHOT_DATE="${SNAPSHOT_DATE:-$(date +%Y-%m-%d)}"
PREFIX="batches/public-merged/${SNAPSHOT_DATE}"
OUTPUT="snapshot-${SNAPSHOT_DATE}.json"
HF_TOKEN="${HF_TOKEN:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/generate-file-list.py"

if [[ ! -f "${PYTHON_SCRIPT}" ]]; then
    echo "ERROR: ${PYTHON_SCRIPT} not found" >&2
    exit 1
fi

echo "Generating snapshot for ${PREFIX} in ${REPO}..."
python3 "${PYTHON_SCRIPT}" \
    --repo "${REPO}" \
    --prefix "${PREFIX}" \
    --output "${OUTPUT}" \
    ${HF_TOKEN:+--token "${HF_TOKEN}"}

echo "Snapshot written to ${OUTPUT}"
```

### Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

# ... existing header ...

SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

resolve_files() {
    if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
        # Use snapshot: read JSON and extract files
        python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for p in data['files']:
    print(p)
" "${SNAPSHOT_FILE}"
    else
        # Fallback: list repo tree (avoid recursive on big repos)
        python3 -c "
from huggingface_hub import HfApi
import sys
api = HfApi()
prefix = sys.argv[1]
tree = api.list_repo_tree(sys.argv[2], path=prefix, recursive=False)
for item in tree:
    if item.type == 'file':
        print(item.rfilename)
" "${PREFIX:-batches/public-merged/$(date +%Y-%m-%d)}" "${REPO}"
    fi
}

# Deterministic shard assignment by slug hash
assign_shard() {
    local slug="$1"
    local hash
    # Use md5 to map slug -> [0, TOTAL_SHARDS)
    hash=$(echo -n "$slug" | md5sum | awk '{print $1}')
    local dec=$((16#${
