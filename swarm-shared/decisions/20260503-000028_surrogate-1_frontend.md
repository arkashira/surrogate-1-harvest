# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit risks during ingestion and reduces per-shard overhead.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Accepts `DATE` (YYYY-MM-DD) and optional `REPO` (default: `axentx/surrogate-1-training-pairs`)  
   - Uses `huggingface_hub` Python CLI (`huggingface-cli`) or a small inline Python script to call `list_repo_tree(path=date_folder, recursive=True)`  
   - Outputs `snapshot-<date>.json` containing `{date, repo, files: [{path, size, sha, url}]}`  
   - Computes deterministic shard assignment for each file (`hash(slug) % 16`) so shards can pre-filter work  
   - Saves to `snapshots/` and prints path to stdout

2. **Update `bin/dataset-enrich.sh`** (30m)  
   - Add optional `SNAPSHOT` env var; if provided, skip `list_repo_tree` and read file list from snapshot  
   - Each shard filters files by pre-computed `shard_id == SHARD_ID` to avoid runtime tree walking  
   - Keep fallback to live API if snapshot missing (backward compatibility)

3. **Add `bin/lib/snapshot.py`** (25m)  
   - Small reusable module with `list_date_snapshot(date, repo)` returning the JSON structure  
   - Uses `huggingface_hub.list_repo_tree(repo_id, path=date, recursive=True)`  
   - Builds CDN URLs: `f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"`  
   - Handles pagination and 429 with exponential backoff (respect 360s wait)

4. **Update GitHub Actions matrix** (20m)  
   - Add a pre-job step that runs `snapshot.sh` for the target date and uploads artifact `snapshot-<date>.json`  
   - Each shard job downloads the artifact and sets `SNAPSHOT` env var  
   - This ensures all 16 shards use the exact same file list without additional API calls

5. **Add training script support** (20m)  
   - Create `scripts/train-cdn-only.py` that reads the snapshot and uses `wget`/`curl` or Python `requests` to stream parquet files directly from CDN (no HF auth)  
   - Projects only `{prompt, response}` fields at parse time (avoids mixed-schema pyarrow errors)  
   - Supports deterministic shuffling via hash of filename for reproducible epochs

6. **Validation & docs** (10m)  
   - Add `README-SNAPSHOT.md` with usage examples  
   - Smoke test: run snapshot for yesterday, verify 16-shard filtering matches total file count

---

## Code Snippets

### `bin/lib/snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate a snapshot of dataset files for a given date folder.
Usage: python bin/lib/snapshot.py --date 2026-04-29 --repo axentx/surrogate-1-training-pairs
"""
import argparse
import hashlib
import json
import os
import sys
import time
from typing import Dict, List

from huggingface_hub import HfApi, RepositoryNotFoundError

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def hf_sha256_to_hex(tree_sha: str) -> str:
    """HF tree SHA is base64-like; normalize to hex for consistency."""
    # tree.sha from list_repo_tree is a git tree SHA (hex). Use as-is.
    return tree_sha

def list_date_snapshot(date: str, repo_id: str) -> Dict:
    api = HfApi()
    path = date  # e.g. "2026-04-29"
    files = []
    cursor = None
    retries = 0

    while True:
        try:
            tree = api.list_repo_tree(
                repo_id=repo_id,
                path=path,
                recursive=True,
                cursor=cursor,
            )
            for item in tree:
                if item.type == "file":
                    files.append({
                        "path": item.path,
                        "size": getattr(item, "size", 0),
                        "sha": hf_sha256_to_hex(getattr(item, "sha", "")),
                        "url": CDN_TEMPLATE.format(repo=repo_id, path=item.path),
                    })
            cursor = getattr(tree, "cursor", None)
            if not cursor:
                break
            retries = 0
        except RepositoryNotFoundError:
            raise
        except Exception as exc:
            retries += 1
            if retries > 5:
                raise
            wait = 360 if getattr(exc, "status_code", None) == 429 else (2 ** retries)
            print(f"Retry {retries}/{5} after {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)

    # Deterministic shard assignment by filename slug
    for f in files:
        slug = os.path.splitext(os.path.basename(f["path"]))[0]
        f["shard_id"] = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % 16

    snapshot = {
        "date": date,
        "repo": repo_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "total_files": len(files),
    }
    return snapshot

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate dataset snapshot for a date folder.")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs", help="HF dataset repo")
    parser.add_argument("--out", help="Output JSON path (default: snapshots/snapshot-<date>.json)")
    args = parser.parse_args()

    snapshot = list_date_snapshot(args.date, args.repo)
    out_path = args.out or f"snapshots/snapshot-{args.date}.json"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(out_path)

if __name__ == "__main__":
    main()
```

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:?Required: DATE=YYYY-MM-DD}"
OUTDIR="${OUTDIR:-snapshots}"
OUTFILE="${OUTFILE:-$OUTDIR/snapshot-$DATE.json}"

mkdir -p "$OUTDIR"

python3 "$(dirname "$0")/lib/snapshot.py" \
  --date "$DATE" \
  --repo "$REPO" \
  --out "$OUTFILE"
```

### `bin/dataset-enrich.sh` (excerpt — integrate snapshot)
```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${SHARD_ID:?Required}"
SNAPSHOT="${SNAPSHOT:-}"
DATE="${DATE:?Required}"
REPO="${REPO:-axentx/surrogate-1-training-pairs}"

if [[ -n "$SNAPSHOT" && -f "$SNAPSHOT" ]]; then
  echo "Using snapshot: $SNAPSHOT"
  mapfile -t FILES < <(
    python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for f in data['files']:
    if f['shard_id'] == ${SHARD_ID}:
        print(f['url'])
" "$SNAPSHOT"
  )
else
  echo "No snapshot provided; listing via HF API (fallback)"
  # Existing HF API listing logic here (keep as fallback)
  # ...
fi

# Continue processing FILES array with CDN downloads
```

### GitHub Actions pre-job (excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path:
