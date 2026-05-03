# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing — eliminates HF API rate-limit risk during ingestion and ensures deterministic file list for all 16 shards.

### Why this now
- Prevents 429s from recursive `list_repo_files` during parallel runs
- Enables CDN-only fetches (bypasses `/api/` auth checks) per the key insight
- Single Mac-side API call after rate-limit window → embed in workflow → Lightning training uses CDN-only
- Fits <2h: one script + workflow bump + small Python helper

---

### 1. Create `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <date> [--output <path>]
# Produces: snapshots/<date>/files.json  (list of file paths for CDN fetch)
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots/${DATE}"
OUTFILE="${2:-${OUTDIR}/files.json}"

mkdir -p "$(dirname "${OUTFILE}")"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Listing ${REPO} for date=${DATE} ..."

# Single API call: non-recursive per folder to avoid pagination explosion
# We list top-level then one level down for date folder.
# If date folder has subfolders, include them (non-recursive per subfolder).
python3 - "$REPO" "$DATE" "$OUTFILE" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

REPO, DATE, OUTFILE = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()

files = []
# List top-level once
try:
    top = api.list_repo_tree(repo_id=REPO, path="", recursive=False)
except Exception as e:
    print(f"Top list failed: {e}", file=sys.stderr)
    sys.exit(1)

# Find date folder
date_entries = [e for e in top if e.path == DATE or e.path.startswith(f"{DATE}/")]
if not date_entries:
    # If date folder doesn't exist yet, produce empty manifest
    manifest = {"date": DATE, "files": [], "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z"}
    os.makedirs(os.path.dirname(OUTFILE), exist_ok=True)
    with open(OUTFILE, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"No folder for {DATE}; wrote empty manifest to {OUTFILE}")
    sys.exit(0)

# We expect a folder named exactly DATE
path = DATE
entries = api.list_repo_tree(repo_id=REPO, path=path, recursive=False)

# Collect all file paths (non-recursive within date folder)
for e in entries:
    if e.type == "file":
        files.append(e.path)
    elif e.type == "directory":
        # One-level deeper (still non-recursive)
        try:
            sub = api.list_repo_tree(repo_id=REPO, path=e.path, recursive=False)
            for s in sub:
                if s.type == "file":
                    files.append(s.path)
        except Exception as ex:
            print(f"Warning: failed to list {e.path}: {ex}", file=sys.stderr)

# Sort for deterministic shard assignment
files.sort()
manifest = {
    "date": DATE,
    "files": files,
    "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "cdn_base": f"https://huggingface.co/datasets/{REPO}/resolve/main"
}
os.makedirs(os.path.dirname(OUTFILE), exist_ok=True)
with open(OUTFILE, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Wrote {len(files)} files to {OUTFILE}")
PY

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Snapshot saved to ${OUTFILE}"
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

### 2. Create small Python helper for shards to use manifest (`lib/manifest.py`)

```python
# lib/manifest.py
import json
import hashlib
from pathlib import Path
from typing import List, Tuple

def load_manifest(manifest_path: str) -> dict:
    with open(manifest_path) as f:
        return json.load(f)

def shard_files(files: List[str], shard_id: int, total_shards: int = 16) -> List[str]:
    """Deterministic shard assignment by slug hash."""
    shard_files = []
    for fpath in files:
        # Use filename stem as slug; fallback to full path
        slug = Path(fpath).stem or fpath
        h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
        if (h % total_shards) == shard_id:
            shard_files.append(fpath)
    return shard_files

def cdn_url(manifest: dict, file_path: str) -> str:
    base = manifest.get("cdn_base", "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main")
    return f"{base}/{file_path}"
```

---

### 3. Update `bin/dataset-enrich.sh` to accept manifest and use CDN URLs

Modify the worker script to optionally take a manifest file and use CDN URLs instead of HF dataset streaming for listed files.

Key changes (add near top):

```bash
# bin/dataset-enrich.sh  (excerpt)
MANIFEST="${MANIFEST:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

if [ -n "${MANIFEST}" ] && [ -f "${MANIFEST}" ]; then
  echo "Using manifest ${MANIFEST}"
  export USE_MANIFEST=1
else
  export USE_MANIFEST=0
fi
```

Later, in the Python processing section, switch behavior:

```python
# Inline python3 <<'PY' in dataset-enrich.sh (conceptual)
import os, json, requests, pyarrow.parquet as pq, io
from lib.manifest import load_manifest, shard_files, cdn_url

USE_MANIFEST = int(os.getenv("USE_MANIFEST", "0"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))

if USE_MANIFEST and os.getenv("MANIFEST"):
    manifest = load_manifest(os.environ["MANIFEST"])
    all_files = manifest["files"]
    my_files = shard_files(all_files, SHARD_ID, TOTAL_SHARDS)
    print(f"CDN mode: processing {len(my_files)} files from manifest")
    for fpath in my_files:
        url = cdn_url(manifest, fpath)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # Process parquet from memory without HF dataset
        table = pq.read_table(io.BytesIO(resp.content))
        # ... existing projection to {prompt,response} and dedup ...
else
    # Fallback to existing HF dataset streaming (for backward compat)
    ...
```

---

### 4. Update GitHub Actions workflow (`/.github/workflows/ingest.yml`)

Add a pre-step that generates the snapshot and passes it to each shard.

```yaml
# .github/workflows/ingest.yml (excerpt additions)
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      # Pre-flight snapshot (single job step, shared across shards via artifact)
      - name: Generate snapshot
        id: snapshot
        run: |
          DATE=$(date +%Y-%m-%d)
          ./bin/snapshot.sh "$DATE" snapshots/"$DATE"/files.json

