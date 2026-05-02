# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and reduces per-shard API calls to zero.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` on the target date folder  
   - Outputs `snapshot-<date>.json` containing `{ "files": [...], "repo": "...", "date": "..." }`  
   - Single API call only; respects 429 backoff (360s wait)  
   - Saves to `snapshots/` directory, symlinks latest as `snapshot-latest.json`  
   - Exits non-zero if API fails (so CI fails fast)

2. **Update `bin/dataset-enrich.sh`** (30m)  
   - Accept optional snapshot file path as argument (`-s snapshot-latest.json`)  
   - If provided, reads file list from snapshot instead of calling `list_repo_files`  
   - Each shard deterministically filters 1/16 slice by `hash(slug) % 16 == SHARD_ID`  
   - Downloads via CDN URL `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>` with `curl` (no auth header)  
   - Keeps existing schema normalization and dedup via `lib/dedup.py`

3. **Add lightweight Python helper `lib/snapshot.py`** (20m)  
   - `list_files_via_tree(repo, path, recursive=False)` → list of file paths  
   - `save_snapshot(files, repo, date, out_dir)` with timestamp  
   - Used by `snapshot.sh` and can be imported by training scripts later

4. **Update GitHub Actions `ingest.yml`** (25m)  
   - Add a pre-step job `snapshot` that runs `bin/snapshot.sh` and uploads `snapshot-*.json` as an artifact  
   - In the 16-shard matrix job, download the artifact and pass via `-s` argument  
   - Ensure `HF_TOKEN` is still available for repo write (uploads) but not needed for downloads

5. **Add training script snippet** (10m)  
   - Embed snapshot file list into `train.py` so Lightning Studio does CDN-only fetches  
   - Example: `with open("snapshot-latest.json") as f: files = json.load(f)["files"]` then use direct CDN for each file

6. **Validation & cleanup** (20m)  
   - Run `bin/snapshot.sh` locally to verify JSON shape  
   - Run one shard of `dataset-enrich.sh` with snapshot to confirm CDN download and schema projection  
   - Remove any remaining recursive `list_repo_files` calls

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="snapshots"
DATE=$(date +%Y-%m-%d)
TIMESTAMP=$(date +%H%M%S)

mkdir -p "$OUT_DIR"

python3 - <<PY
import json, os, sys, time
from huggingface_hub import HfApi, RepositoryError

repo = os.environ["REPO"]
out_dir = os.environ["OUT_DIR"]
date = os.environ["DATE"]
timestamp = os.environ["TIMESTAMP"]

api = HfApi()
max_retries = 3
backoff = 360  # seconds

for attempt in range(max_retries):
    try:
        # Non-recursive to avoid pagination explosion
        date_tree = api.list_repo_tree(
            repo=repo, path=date, repo_type="dataset", recursive=False
        )
        break
    except RepositoryError as e:
        if attempt == max_retries - 1:
            print(f"Failed after {max_retries} attempts: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Rate limited (429), sleeping {backoff}s...", file=sys.stderr)
        time.sleep(backoff)

files = [item.rfilename for item in date_tree if not item.rfilename.endswith("/")]

snapshot = {
    "repo": repo,
    "date": date,
    "generated_at": f"{date}T{timestamp}",
    "files": sorted(files)
}

latest_path = os.path.join(out_dir, "snapshot-latest.json")
dated_path = os.path.join(out_dir, f"snapshot-{date}-{timestamp}.json")

os.makedirs(out_dir, exist_ok=True)
with open(latest_path, "w") as f:
    json.dump(snapshot, f, indent=2)
with open(dated_path, "w") as f:
    json.dump(snapshot, f, indent=2)

# Symlink latest for convenience
os.symlink(os.path.basename(dated_path), os.path.join(out_dir, "snapshot-latest.json"), 
           target_is_directory=False, dir_fd=None)

print(f"Snapshot written: {latest_path} ({len(files)} files)")
PY

echo "Snapshot complete."
```

### `lib/snapshot.py`
```python
import json
import os
import time
from pathlib import Path
from typing import List, Dict, Any
from huggingface_hub import HfApi, RepositoryError

api = HfApi()

def list_files_via_tree(repo: str, path: str = "", recursive: bool = False, 
                       max_retries: int = 3, backoff: int = 360) -> List[str]:
    """List files via repo tree with 429 backoff."""
    for attempt in range(max_retries):
        try:
            tree = api.list_repo_tree(
                repo=repo, path=path, repo_type="dataset", recursive=recursive
            )
            return [item.rfilename for item in tree if not item.rfilename.endswith("/")]
        except RepositoryError as e:
            if attempt == max_retries - 1:
                raise
            print(f"Rate limited (429), sleeping {backoff}s...")
            time.sleep(backoff)
    return []

def save_snapshot(files: List[str], repo: str, date: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "repo": repo,
        "date": date,
        "files": sorted(files)
    }
    latest = out_dir / "snapshot-latest.json"
    with open(latest, "w") as f:
        json.dump(snapshot, f, indent=2)
    return latest

def load_snapshot(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)
```

### Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
SNAPSHOT_FILE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -s|--snapshot)
            SNAPSHOT_FILE="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

cd "$(dirname "$0")/.."

python3 - <<PY
import os, json, hashlib, sys, subprocess, tempfile
from pathlib import Path

repo = os.environ["REPO"]
shard_id = int(os.environ["SHARD_ID"])
total_shards = int(os.environ["TOTAL_SHARDS"])
snapshot_file = os.environ.get("SNAPSHOT_FILE", "")

if snapshot_file and Path(snapshot_file).exists():
    with open(snapshot_file) as f:
        manifest = json.load(f)
    files = manifest["files"]
else:
    # fallback: list via API (avoid in production)
    from huggingface_hub import HfApi
    api = HfApi()
    tree = api.list_repo_tree(repo=repo, path="", repo_type="dataset", recursive=False)
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]

# Deterministic shard
