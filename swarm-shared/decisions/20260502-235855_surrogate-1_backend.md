# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and reduces per-shard overhead by replacing recursive `list_repo_files` with a single tree call + CDN fetches.

### Concrete steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Accept `DATE` (YYYY-MM-DD) or default to today.  
   - Call `huggingface_hub` Python helper to run `list_repo_tree(path=f"public-merged/{DATE}", recursive=False)` once.  
   - Save JSON to `snapshots/public-merged-{DATE}.json` containing `{ "date": "...", "files": [...], "generated_at": "...", "repo": "axentx/surrogate-1-training-pairs" }`.  
   - Make executable (`chmod +x`).

2. **Create `bin/lib/snapshot.py`** (20m)  
   - Small reusable module: `list_date_folder(date)` → list of filenames; `save_snapshot(date, files)`; `load_snapshot(date)`.  
   - Use `HF_TOKEN` optional (public repo doesn’t require auth for tree/list).  
   - Handle pagination (tree is non-paginated for folders; fallback to `list_repo_tree` recursive=False).

3. **Update `bin/dataset-enrich.sh`** (30m)  
   - Add pre-flight: if snapshot for target date exists and is <1h old, reuse; else run `snapshot.sh`.  
   - Replace per-shard recursive listing with deterministic shard-slicing over the snapshot file list:  
     - Sort files lexicographically, assign each file to `hash(filename) % 16` → `SHARD_ID`.  
     - Each runner processes only its shard’s subset.  
   - Downloads use CDN URLs: `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/public-merged/${DATE}/${file}` (no auth, bypasses API rate limits).  
   - Keep existing schema normalization and dedup via `lib/dedup.py`.

4. **Update GitHub Actions matrix** (15m)  
   - No change to 16-shard matrix.  
   - Add a single “snapshot” job (or step) that runs before the matrix, generates snapshot, and uploads it as an artifact for all shards to download.  
   - Alternatively, each shard can run snapshot locally if missing (idempotent, cheap).

5. **Add training integration stub** (20m)  
   - Create `bin/train-file-list.sh` that outputs newline-separated CDN URLs for a given date (for embedding into Lightning training).  
   - Document pattern: Mac runs snapshot once, embeds file list into `train.py`; Lightning Studio uses CDN-only `open(url)` via `datasets` or custom `IterableDataset` that never calls HF API.

6. **Tests & safety** (20m)  
   - Add dry-run flag to snapshot (`--dry-run`) to validate tree access.  
   - Validate shard assignment is deterministic (unit test in `bin/lib/test_shard.py`).  
   - Ensure script exits non-zero on tree/list failures.

7. **Documentation** (10m)  
   - Update README with snapshot usage and CDN bypass rationale.  
   - Add note about HF rate limits and the 360s backoff if 429 still occurs on tree call.

---

## Code snippets

### `bin/lib/snapshot.py`

```python
#!/usr/bin/env python3
"""
Snapshot utilities for surrogate-1 dataset folders.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
SNAPSHOT_DIR = Path(__file__).parents[2] / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True, parents=True)

api = HfApi()

def list_date_folder(date: str):
    """
    List files in public-merged/{date}/ (non-recursive).
    Returns list of filenames (relative to repo root).
    """
    prefix = f"public-merged/{date}/"
    try:
        files = list_repo_tree(repo_id=REPO, path=prefix, repo_type="dataset", recursive=False)
    except Exception as e:
        raise RuntimeError(f"Failed to list repo tree for {prefix}: {e}") from e
    # items are dicts with 'path'
    paths = [item["path"] for item in files if item.get("type") == "file"]
    return sorted(paths)

def save_snapshot(date: str, files):
    snapshot = {
        "repo": REPO,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out = SNAPSHOT_DIR / f"public-merged-{date}.json"
    out.write_text(json.dumps(snapshot, indent=2))
    return out

def load_snapshot(date: str):
    p = SNAPSHOT_DIR / f"public-merged-{date}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())

def main():
    if len(sys.argv) < 2:
        print("Usage: snapshot.py <YYYY-MM-DD>")
        sys.exit(1)
    date = sys.argv[1]
    print(f"Listing public-merged/{date}/ ...")
    files = list_date_folder(date)
    out = save_snapshot(date, files)
    print(f"Saved {len(files)} files to {out}")

if __name__ == "__main__":
    main()
```

### `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# Generate a file snapshot for a date folder to enable CDN-only ingestion.
# Usage: snapshot.sh [YYYY-MM-DD]
set -euo pipefail

cd "$(dirname "$0")/.."

DATE="${1:-$(date +%F)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Generating snapshot for ${DATE} ..."
"${SCRIPT_DIR}/lib/snapshot.py" "${DATE}"

echo "Done."
```

### Updated `bin/dataset-enrich.sh` (key excerpt)

```bash
#!/usr/bin/env bash
# ... existing header ...
set -euo pipefail

cd "$(dirname "$0")/.."

SHARD_ID="${SHARD_ID:?required}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE="${DATE:-$(date +%F)}"

SNAPSHOT="../snapshots/public-merged-${DATE}.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Pre-flight: generate snapshot if missing or stale (>1h)
if [[ ! -f "${SNAPSHOT}" ]] || [[ $(find "${SNAPSHOT}" -mmin +60 -print) ]]; then
  echo "Snapshot missing or stale; generating..."
  "${SCRIPT_DIR}/snapshot.sh" "${DATE}"
fi

# Load file list and assign deterministic shard
mapfile -t ALL_FILES < <(python3 -c "
import json, sys
d=json.load(open('${SNAPSHOT}'))
for f in d['files']:
    print(f)
")

# Deterministic shard assignment by filename
SHARD_FILES=()
for f in "${ALL_FILES[@]}"; do
  # Stable numeric hash from filename (POSIX-compatible)
  HASH=$(echo -n "$f" | cksum | awk '{print $1}')
  if (( HASH % TOTAL_SHARDS == SHARD_ID )); then
    SHARD_FILES+=("$f")
  fi
done

echo "Shard ${SHARD_ID}/${TOTAL_SHARDS} processing ${#SHARD_FILES[@]} files."

# Process each file via CDN (no auth, bypasses API rate limits)
BASE_CDN="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"
for rel_path in "${SHARD_FILES[@]}"; do
  url="${BASE_CDN}/${rel_path}"
  echo "Fetching ${url} ..."
  # Stream download and normalize; keep existing schema handling
  # Example: python3 normalize.py --url "$url"
