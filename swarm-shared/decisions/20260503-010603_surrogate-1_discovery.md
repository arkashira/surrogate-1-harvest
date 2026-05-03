# surrogate-1 / discovery

## Highest-value incremental improvement (≤2h)

**Goal**: Eliminate HF API rate-limit failures and HF Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.

**Why this wins**:
- Avoids `list_repo_files` recursive pagination (100× API calls) and per-file metadata requests.
- CDN downloads (`/resolve/main/...`) bypass `/api/` auth checks and have much higher rate limits.
- Single manifest JSON per date folder lets Lightning training run with zero HF API calls during data load.
- Keeps 16-shard parallel ingestion architecture unchanged — only the discovery/fetch layer changes.

---

## Implementation plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | Me | 15m | Inspect `bin/dataset-enrich.sh` and confirm current ingestion pattern (likely uses `load_dataset` or recursive listing). |
| 2 | Me | 20m | Add `bin/build-manifest.py` — takes `date` and optional `repo`, runs `list_repo_tree(recursive=False)` per subfolder, emits `manifest-{date}.json` with `{path, size, etag, url}`. |
| 3 | Me | 20m | Modify `bin/dataset-enrich.sh` to: (a) accept manifest file or date arg, (b) shard by `hash(slug) % 16 == SHARD_ID`, (c) download via CDN URL with `curl`/`requests` (no auth), (d) parse only `{prompt,response}` fields, (e) stream to `shard-<N>-<ts>.jsonl`. |
| 4 | Me | 15m | Add lightweight dedup guard: skip if `md5` already in central dedup store (reuse `lib/dedup.py`). |
| 5 | Me | 20m | Add fallback: if CDN 404, skip file (log); if API needed for private repos, use token but keep calls minimal. |
| 6 | Me | 20m | Update README section with usage: `./bin/build-manifest.py 2026-05-03 > manifests/2026-05-03.json` then run workflow or local test. |
| 7 | Me | 10m | Smoke test: run one shard locally against a small public dataset folder. |
| 8 | Me | 10m | Commit and push. |

Total: ~1h50m (buffer included).

---

## Code snippets

### 1) `bin/build-manifest.py` (new)

```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for a date folder in a HuggingFace dataset repo.
Usage:
  ./bin/build-manifest.py 2026-05-03 > manifests/2026-05-03.json
"""
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else datetime.utcnow().strftime("%Y-%m-%d")
SUBPATH = DATE_FOLDER  # top-level folder per date

api = HfApi()
entries = api.list_repo_tree(
    repo_id=REPO,
    path=SUBPATH,
    repo_type="dataset",
    recursive=False,
)

manifest = {
    "repo": REPO,
    "date": DATE_FOLDER,
    "generated_at": datetime.utcnow().isoformat() + "Z",
    "files": [],
}

for entry in entries:
    if entry.type != "file":
        continue
    # CDN URL bypasses API auth/rate limits
    cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{SUBPATH}/{entry.path}"
    manifest["files"].append(
        {
            "path": f"{SUBPATH}/{entry.path}",
            "size": getattr(entry, "size", None),
            "lfs": getattr(entry, "lfs", None),
            "cdn_url": cdn_url,
        }
    )

json.dump(manifest, sys.stdout, indent=2)
```

Make executable:

```bash
chmod +x bin/build-manifest.py
```

---

### 2) Updated `bin/dataset-enrich.sh` (key changes)

```bash
#!/usr/bin/env bash
#
# dataset-enrich.sh
# Sharded ingestion worker: uses CDN manifest + zero HF API during fetch.
#
# Required env:
#   SHARD_ID      (0-15)
#   HF_TOKEN      (write token for uploads only)
#   MANIFEST_FILE (optional) path to manifest-<date>.json
#   DATE_FOLDER   (optional) e.g. 2026-05-03
#
set -euo pipefail
SHELL=/bin/bash

cd "$(dirname "$0")/.."

: "${SHARD_ID:?required}"
: "${HF_TOKEN:?required}"
: "${DATE_FOLDER:?required}"

MANIFEST_FILE="${MANIFEST_FILE:-manifests/${DATE_FOLDER}.json}"
OUT_DIR="batches/public-merged/${DATE_FOLDER}"
TS=$(date -u +"%H%M%S")
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "$OUT_FILE")"

# If manifest missing, build it once (single API call per worker group)
if [[ ! -f "$MANIFEST_FILE" ]]; then
  echo "Building manifest for ${DATE_FOLDER}..."
  python3 bin/build-manifest.py "$DATE_FOLDER" > "$MANIFEST_FILE"
fi

TOTAL=$(jq '.files | length' "$MANIFEST_FILE")
echo "Processing shard ${SHARD_ID}/${TOTAL} files from ${DATE_FOLDER}"

# Stream files assigned to this shard
jq -c --argjson sid "$SHARD_ID" '.files[] | select((.path | hash_slug) % 16 == $sid)' "$MANIFEST_FILE" | while IFS= read -r item; do
  url=$(echo "$item" | jq -r '.cdn_url')
  path=$(echo "$item" | jq -r '.path')
  echo "Fetching ${path}..."

  # CDN fetch (no auth header)
  tmp=$(mktemp)
  if ! curl -fsSL --retry 3 --max-time 60 "$url" -o "$tmp"; then
    echo "WARN: CDN fetch failed for ${path}, skipping"
    rm -f "$tmp"
    continue
  fi

  # Project to {prompt,response} and normalize per known schemas
  # Keep lib/dedup.py for cross-source md5 dedup (central store on HF Space)
  python3 -c "
import sys, json, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, register_hash

def extract_pairs(obj):
    # Heuristic projection for known schemas; extend as needed
    prompt = obj.get('prompt') or obj.get('input') or obj.get('question') or ''
    response = obj.get('response') or obj.get('output') or obj.get('answer') or ''
    if not prompt or not response:
        return []
    # Normalize whitespace minimally
    prompt = ' '.join(str(prompt).split())
    response = ' '.join(str(response).split())
    return [{'prompt': prompt, 'response': response}]

try:
    data = json.load(open('$tmp', 'r'))
except Exception:
    # try line-delimited
    lines = Path('$tmp').read_text().strip().splitlines()
    data = [json.loads(l) for l in lines if l.strip()]

pairs = []
for record in (data if isinstance(data, list) else [data]):
    pairs.extend(extract_pairs(record))

for p in pairs:
    blob = json.dumps(p, sort_keys=True, separators=(',', ':'))
    h = hashlib.md5(blob.encode()).hexdigest()
    if not is_duplicate(h):
        register_hash(h)
        print(blob)
" >> "$OUT_FILE" 2>/dev/null || true

  rm -f "$tmp"
done

# Upload shard output to dataset repo (single
