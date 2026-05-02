# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit failures during ingestion and reduces per-shard overhead.

### Steps
1. Create `bin/snapshot.sh` — uses `huggingface_hub` to `list_repo_tree` (non-recursive) for a given date folder, outputs `snapshot-<date>.json` with CDN URLs.
2. Update `bin/dataset-enrich.sh` to accept an optional snapshot file; if provided, iterate local manifest instead of calling `list_repo_files` per shard.
3. Add a small Python helper (`lib/manifest.py`) to parse snapshot and produce `{cdn_url, path, slug}` entries.
4. Modify shard worker logic to download via `https://huggingface.co/datasets/.../resolve/main/...` (no auth) using the manifest.
5. Update workflow to generate snapshot once (single job) and pass it to the 16 shard matrix jobs via `artifacts` or `outputs`.

### Code Snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate a file manifest for a date folder in axentx/surrogate-1-training-pairs
# Usage: bin/snapshot.sh <date> [output.json]
# Example: bin/snapshot.sh 2026-05-02

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-snapshot-${DATE}.json}"

echo "Listing dataset tree for ${REPO}/batches/public-merged/${DATE} ..."

python3 - <<PY
import json, os, sys
from huggingface_hub import HfApi

repo = os.environ.get("REPO", "$REPO")
date = os.environ.get("DATE", "$DATE")
out = os.environ.get("OUT", "$OUT")

api = HfApi()
# non-recursive listing of the date folder
tree = api.list_repo_tree(
    repo=repo,
    path=f"batches/public-merged/{date}",
    recursive=False,
)

files = []
for item in tree:
    if item.type != "file":
        continue
    # CDN URL (no auth)
    cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
    files.append({
        "path": item.path,
        "cdn_url": cdn,
        "size": getattr(item, "size", None),
    })

os.makedirs(os.path.dirname(out) if os.path.dirname(out) else ".", exist_ok=True)
with open(out, "w") as f:
    json.dump({"date": date, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files to {out}")
PY

echo "Snapshot saved to ${OUT}"
```

#### `lib/manifest.py`
```python
# lib/manifest.py
import json
import os
from typing import List, Dict

def load_manifest(path: str) -> List[Dict]:
    with open(path) as f:
        data = json.load(f)
    return data.get("files", [])

def iter_cdn_urls(manifest_path: str):
    for entry in load_manifest(manifest_path):
        yield entry["cdn_url"], entry["path"]
```

#### Update `bin/dataset-enrich.sh` (minimal change)
```bash
# Near top of dataset-enrich.sh, after argument parsing
MANIFEST="${MANIFEST:-}"
if [ -n "${MANIFEST}" ] && [ -f "${MANIFEST}" ]; then
  echo "Using manifest ${MANIFEST}"
  # Use python helper to stream CDN URLs instead of HF API listing
  URLS=$(python3 -c "
import sys, json
with open('${MANIFEST}') as f:
    files = json.load(f).get('files', [])
for f in files:
    print(f['cdn_url'])
")
else
  # fallback to existing behavior (HF API listing)
  URLS=$(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree('axentx/surrogate-1-training-pairs', path='batches/public-merged/${DATE}', recursive=False)
for item in tree:
    if item.type == 'file':
        print(f'https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{item.path}')
")
fi

# Then iterate over $URLS as before (download via CDN URLs)
```

#### Workflow snippet (`.github/workflows/ingest.yml`) — high-level
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      manifest: ${{ steps.set.outputs.manifest }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_hub
      - run: bin/snapshot.sh ${{ github.event.inputs.date || format('2026-05-{0}', github.run_number) }} snapshot.json
      - name: Set output
        id: set
        run: echo "manifest=snapshot.json" >> $GITHUB_OUTPUT
      - uses: actions/upload-artifact@v4
        with:
          name: snapshot
          path: snapshot.json

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: snapshot
      - run: bin/dataset-enrich.sh ${{ matrix.shard }} 16 MANIFEST=snapshot.json
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

### Notes
- Snapshot generation is a single, cheap API call (`list_repo_tree` non-recursive) and avoids per-shard `list_repo_files` pagination and rate limits.
- Shards consume only CDN URLs (no Authorization header), bypassing `/api/` rate limits entirely.
- Manifest can be reused across workflow runs if stored as artifact or committed for reproducibility.
- If snapshot is unavailable, fallback preserves existing behavior.
