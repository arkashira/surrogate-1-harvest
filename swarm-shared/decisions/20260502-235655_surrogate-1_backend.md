# surrogate-1 / backend

## Final Synthesized Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate limits during ingestion/training and reduces 429 errors.

### Core Design Decisions (Resolved Contradictions)
1. **Single snapshot file** (not per-folder) with deterministic sorting for stable sharding across all workers.
2. **CDN-only downloads** via `hf_hub_download` (not raw `curl`) — handles auth, retries, and cache automatically while using CDN under the hood.
3. **One `snapshot` job** producing an artifact consumed by all shards, avoiding redundant API calls.
4. **Graceful fallback** to HF API only if snapshot is missing (should never happen in CI).

---

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (15m)  
   - Runs once per workflow; outputs `snapshot-<date>.json`  
   - Uses `huggingface_hub` to list top-level date folders, then files per folder  
   - Deterministic sort; includes `cdn_base` and full file list with URLs

2. **Create `lib/snapshot.py`** (15m)  
   - `build_snapshot(repo_id, out_path, date_prefix=None)`  
   - Returns `{"repo":..., "date":..., "cdn_base":..., "files":[...], "urls":[...]}`  
   - Used by `snapshot.sh` and tests

3. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accepts `SNAPSHOT_FILE` env var; if present, reads file list from snapshot  
   - Uses `huggingface_hub.hf_hub_download` for each file (CDN + auth handled)  
   - Falls back to HF API only if snapshot missing (with warning)

4. **Add shard slicing logic** (15m)  
   - Deterministic slice: `urls[shard_id::total_shards]`  
   - Embedded in `dataset-enrich.sh` via Python one-liner or small helper

5. **Update GitHub Actions workflow** (20m)  
   - Add `snapshot` job producing artifact `snapshot-<date>.json`  
   - `ingest-shard` matrix job downloads artifact and passes `SNAPSHOT_FILE` path + `SHARD_ID`/`TOTAL_SHARDS` env vars

6. **Validation** (25m)  
   - Run `snapshot.sh` locally; verify JSON structure and URL format  
   - Run one shard with snapshot; confirm no `datasets` API calls and successful CDN downloads

---

### Code Snippets

#### 1. `lib/snapshot.py`
```python
#!/usr/bin/env python3
"""
Build a deterministic snapshot of dataset files with CDN URLs.
"""
import argparse
import json
import os
from typing import Dict, List
from huggingface_hub import HfApi

def build_snapshot(repo_id: str, out_path: str, date_prefix: str = None) -> Dict:
    api = HfApi()
    # List top-level folders (dates)
    root_tree = api.list_repo_tree(repo_id=repo_id, path="", recursive=False)
    folders = sorted([
        item.rfilename for item in root_tree
        if item.type == "directory" and (not date_prefix or item.rfilename.startswith(date_prefix))
    ])

    all_files = []
    for folder in folders:
        files = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
        file_names = sorted([
            f.rfilename for f in files if f.type == "file"
        ])
        all_files.extend([f"{folder}/{f}" for f in file_names])

    cdn_base = f"https://huggingface.co/datasets/{repo_id}/resolve/main"
    urls = [f"{cdn_base}/{f}" for f in all_files]

    snapshot = {
        "repo": repo_id,
        "date": date_prefix or "all",
        "cdn_base": cdn_base,
        "files": all_files,
        "urls": urls,
    }

    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)

    return snapshot

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build dataset snapshot")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", help="Date prefix (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    snap = build_snapshot(args.repo, args.out, args.date)
    print(f"Snapshot: {len(snap['files'])} files -> {args.out}")
```

#### 2. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
OUTDIR="${OUTDIR:-snapshots}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUTFILE="${OUTDIR}/snapshot-${DATE}.json"

mkdir -p "${OUTDIR}"

python3 lib/snapshot.py \
    --repo "${REPO}" \
    --date "${DATE}" \
    --out "${OUTFILE}"

echo "Snapshot created: ${OUTFILE}"
```

#### 3. `bin/dataset-enrich.sh` (updated section)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

# Determine file list and URLs
if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
    echo "Using snapshot: ${SNAPSHOT_FILE}"
    mapfile -t URLS < <(python3 -c "
import json, os, sys
with open('${SNAPSHOT_FILE}') as f:
    data = json.load(f)
shard = int(os.environ.get('SHARD_ID', '0'))
total = int(os.environ.get('TOTAL_SHARDS', '16'))
urls = data['urls']
for u in urls[shard::total]:
    print(u)
")
else
    echo "WARNING: No snapshot file; falling back to HF API (may hit rate limits)"
    mapfile -t URLS < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_files('${REPO}')
for f in sorted(files):
    if f.endswith('.parquet') or f.endswith('.jsonl'):
        print(f'https://huggingface.co/datasets/${REPO}/resolve/main/{f}')
")
fi

if [[ ${#URLS[@]} -eq 0 ]]; then
    echo "ERROR: No files to process"
    exit 1
fi

echo "Shard ${SHARD_ID}/${TOTAL_SHARDS} processing ${#URLS[@]} files"

# Download using hf_hub_download (CDN + auth handled)
for url in "${URLS[@]}"; do
    # Extract repo-relative path from URL
    rel_path="${url#*resolve/main/}"
    python3 -c "
from huggingface_hub import hf_hub_download
import os
repo = os.environ.get('REPO', 'axentx/surrogate-1-training-pairs')
path = '${rel_path}'
hf_hub_download(repo_id=repo, filename=path, local_dir='.cache', local_dir_use_symlinks=False)
print(f'Downloaded: {path}')
"
done
```

#### 4. `.github/workflows/ingest.yml` (partial diff)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-file: ${{ steps.set.outputs.file }}
      date: ${{ steps.date.outputs.date }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
