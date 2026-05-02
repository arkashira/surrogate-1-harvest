# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit pressure during ingestion, guarantees deterministic file sets per run, and ensures reproducible training manifests.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(recursive=False)` for the target date folder.  
   - Outputs `snapshot-<date>.json` with `{"repo","date","generated_at","files":[{"path","size"}]}`.  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`, `chmod +x`.  
   - Validates HF token presence; exits non-zero on 429 with retry-after parsing.

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accept optional `SNAPSHOT_FILE` env var. If provided, skip `list_repo_files` and read file list from snapshot; otherwise fall back to live listing for backward compatibility.  
   - Keep existing deterministic shard assignment (`hash(path) % 16 == SHARD_ID`).

3. **Add `lib/file_list.py`** (20m)  
   - Loads snapshot JSON, filters by shard ID, yields local/remote paths.  
   - Uses `hf_hub_download` for CDN downloads (no auth header on resolve URLs).  
   - Projects to `{prompt, response}` only at parse time to avoid pyarrow CastError on mixed schemas.

4. **Update GitHub Actions matrix** (10m)  
   - Add optional `snapshot` job that runs `bin/snapshot.sh` before the 16-shard matrix.  
   - Artifact upload of `snapshot-<date>.json`.  
   - Shard jobs download artifact and set `SNAPSHOT_FILE`.

5. **Add training script integration stub** (20m)  
   - Create `bin/make_train_manifest.py` that consumes snapshot and emits `train-files-<date>.json` for Lightning Studio.  
   - Embeds CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) so training does zero API calls.

6. **Tests & docs** (20m)  
   - Add `--dry-run` flag to snapshot script to verify listing without writes.  
   - Update README with usage:  
     ```bash
     # Generate snapshot once per day
     ./bin/snapshot.sh --repo axentx/surrogate-1-training-pairs --date 2026-05-02
     # Run shard with snapshot
     SNAPSHOT_FILE=snapshot-2026-05-02.json ./bin/dataset-enrich.sh
     ```

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
# snapshot.sh — list dataset files once for CDN-only training
# Usage: ./bin/snapshot.sh --repo <owner>/<dataset> --date YYYY-MM-DD [--out <path>] [--dry-run]

REPO=""
DATE=""
OUT=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo)  REPO="$2"; shift 2 ;;
    --date)  DATE="$2"; shift 2 ;;
    --out)   OUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

: "${REPO:?required}"
: "${DATE:?required}"
OUT="${OUT:-snapshot-${DATE}.json}"

python3 - <<PY
import os, json, sys, time
from huggingface_hub import HfApi
from datetime import datetime

repo = "${REPO}"
date = "${DATE}"
out = "${OUT}"
dry_run = ${DRY_RUN}
api = HfApi()

try:
    tree = api.list_repo_tree(repo=repo, path=date, repo_type="dataset", recursive=False)
except Exception as e:
    if "429" in str(e):
        print("Rate limited (429). Wait 360s before retry.", file=sys.stderr)
    sys.exit(1)

files = [{"path": f.rfilename, "size": getattr(f, "size", None)} for f in tree if f.rfilename]
snapshot = {
    "repo": repo,
    "date": date,
    "generated_at": datetime.utcnow().isoformat() + "Z",
    "files": files
}

if dry_run:
    print(f"[dry-run] Would write {len(files)} files to {out}")
    sys.exit(0)

os.makedirs(os.path.dirname(out) if os.path.dirname(out) else ".", exist_ok=True)
with open(out, "w") as fp:
    json.dump(snapshot, fp, indent=2)

print(f"Snapshot written to {out} ({len(files)} files)")
PY
```

### `lib/file_list.py`
```python
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any
from huggingface_hub import hf_hub_download

def load_snapshot(path: str) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)

def shard_files(snapshot_path: str, shard_id: int, total_shards: int = 16) -> Iterator[Dict[str, str]]:
    """Yield files assigned to this shard by deterministic hash."""
    snap = load_snapshot(snapshot_path)
    for entry in snap["files"]:
        path = entry["path"]
        if hash(path) % total_shards == shard_id:
            yield {
                "remote_path": path,
                "repo": snap["repo"],
                "local_path": hf_hub_download(
                    repo_id=snap["repo"],
                    filename=path,
                    repo_type="dataset",
                )
            }

def cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
```

### `bin/dataset-enrich.sh` (excerpt — integrate snapshot)
```bash
#!/usr/bin/env bash
set -euo pipefail
# If SNAPSHOT_FILE is provided, use it; otherwise fall back to live listing.

SHARD_ID="${SHARD_ID:?required}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
  echo "Using snapshot $SNAPSHOT_FILE for shard $SHARD_ID"
  python3 -c "
import sys, json
from lib.file_list import shard_files
for f in shard_files('$SNAPSHOT_FILE', int('$SHARD_ID')):
    print(f['remote_path'])
" | while read -r remote_path; do
    # Process each file (streaming, project to {prompt,response} only)
    process_one_file "$remote_path"
  done
else
  echo "No snapshot; falling back to live listing (may hit rate limits)"
  # existing logic using huggingface_hub list_repo_files...
fi
```

### `bin/make_train_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for Lightning training.
Usage: python bin/make_train_manifest.py snapshot-2026-05-02.json > train-files-2026-05-02.json
"""
import json, sys
from lib.file_list import load_snapshot, cdn_url

def main():
    snapshot_path = sys.argv[1]
    snap = load_snapshot(snapshot_path)
    manifest = {
        "repo": snap["repo"],
        "date": snap["date"],
        "generated_at": snap["generated_at"],
        "train_files": [cdn_url(snap["repo"], f["path"]) for f in snap["files"]]
    }
    json.dump(manifest, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

### `.github/workflows/ingest.yml` (excerpt — snapshot job)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-name: ${{ steps.set.outputs.name }}
    steps:
      - uses: actions/check
