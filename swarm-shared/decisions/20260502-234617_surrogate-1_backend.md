# surrogate-1 / backend

## Final Synthesized Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and removes `list_repo_files` recursive pagination overhead.

### Why this matters
- HF API rate-limit (1000 req/5min) blocks parallel 16-shard ingestion when each runner calls `list_repo_files` recursively.
- CDN downloads (`/resolve/main/`) bypass auth and have much higher limits.
- Single deterministic file-list snapshot lets each shard pick its 1/16 slice without API calls during processing.
- Fits existing patterns: pre-list once, embed in training script, CDN-only fetches, studio reuse.

---

### Concrete steps (1h 45m total)

| Time | Task |
|------|------|
| 15m | Create `bin/snapshot.sh` — lists top-level date folders via `list_repo_tree`, saves `snapshot.json` with `path`, `sha`, `size`, `date`, `slug` |
| 20m | Update `bin/dataset-enrich.sh` to accept snapshot file as arg; compute deterministic shard assignment from `slug-hash % 16`; skip API calls during run |
| 20m | Add `lib/snapshot.py` helpers: deterministic slug hash, shard assignment, CDN URL builder |
| 20m | Update `.github/workflows/ingest.yml`: add pre-step to generate snapshot, pass `snapshot.json` to each matrix shard via artifact |
| 20m | Add lightweight validation: ensure snapshot freshness (<24h), fallback to API list if snapshot missing (safe default) |
| 30m | Test locally: run snapshot, run two shards, verify CDN-only downloads and no HF API auth calls in data loader |

---

### Code snippets

#### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate a file manifest for axentx/surrogate-1-training-pairs
# Usage: bin/snapshot.sh [--output snapshot.json]

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
OUTFILE="${2:-snapshot.json}"
HF_TOKEN="${HF_TOKEN:-}"

if [[ -n "$HF_TOKEN" ]]; then
  AUTH_HEADER="Authorization: Bearer $HF_TOKEN"
else
  AUTH_HEADER=""
fi

# List top-level date folders (non-recursive) to avoid pagination/rate-limit
# We only need the folder names; CDN downloads don't require auth.
echo "Listing top-level folders in $REPO ..."
folders=$(curl -sS -H "$AUTH_HEADER" \
  "https://huggingface.co/api/datasets/$REPO/tree?recursive=false" | \
  jq -r '.[] | select(.type=="directory") | .path')

# Build manifest: for each date folder, list files via CDN tree (no auth required)
# We use the public tree endpoint per folder (still no auth for public repos).
manifest="[]"
for d in $folders; do
  echo "Scanning $d ..."
  files=$(curl -sS \
    "https://huggingface.co/api/datasets/$REPO/tree?path=$d&recursive=true" | \
    jq -c '.[] | select(.type=="file") | {path: .path, sha: .sha, size: .size}')
  while IFS= read -r f; do
    path=$(echo "$f" | jq -r '.path')
    slug=$(basename "$path" .parquet | sed 's/\.[^.]*$//')
    manifest=$(echo "$manifest" | jq --arg p "$path" --argjson s "$f" --arg slug "$slug" \
      '. + [{"path": $p, "sha": $s.sha, "size": $s.size, "slug": $slug}]')
  done <<< "$files"
done

echo "$manifest" | jq '{generated_at: now|todate, repo: env.REPO, files: .}' > "$OUTFILE"
echo "Snapshot written to $OUTFILE ($(jq '.files | length' "$OUTFILE") files)"
```

#### 2. `lib/snapshot.py`
```python
# lib/snapshot.py
import json
import hashlib
import os
from typing import List, Dict, Any

def load_snapshot(path: str = "snapshot.json") -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)

def deterministic_shard(slug: str, total_shards: int = 16) -> int:
    """Map slug to shard 0..15 deterministically."""
    digest = hashlib.md5(slug.encode()).hexdigest()
    return int(digest, 16) % total_shards

def cdn_url(repo: str, filepath: str) -> str:
    """CDN URL that bypasses HF API auth checks."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{filepath}"

def files_for_shard(snapshot: Dict[str, Any], shard_id: int, total_shards: int = 16) -> List[Dict[str, Any]]:
    return [
        f for f in snapshot.get("files", [])
        if deterministic_shard(f.get("slug", ""), total_shards) == shard_id
    ]
```

#### 3. Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Accepts snapshot file as first arg; falls back to API if missing.

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
SNAPSHOT="${1:-snapshot.json}"

source "$(dirname "$0")/lib/dedup.py"  # if needed for md5 store

if [[ -f "$SNAPSHOT" ]]; then
  echo "Using snapshot $SNAPSHOT"
  mapfile -t FILES < <(python3 -c "
import sys, json
from lib.snapshot import files_for_shard
snapshot = json.load(open('$SNAPSHOT'))
for f in files_for_shard(snapshot, $SHARD_ID):
    print(f['path'])
")
else
  echo "Snapshot not found, falling back to API (may hit rate limits)..."
  # fallback: use huggingface_hub to list files (existing behavior)
  mapfile -t FILES < <(python3 -c "
from huggingface_hub import list_repo_files
for f in list_repo_files('$REPO'):
    print(f)
")
fi

for relpath in "${FILES[@]}"; do
  url="https://huggingface.co/datasets/$REPO/resolve/main/$relpath"
  echo "Processing $relpath via CDN: $url"
  # Download via CDN (no auth), project to {prompt,response}, dedup, append to shard output
  curl -sSL "$url" -o "/tmp/$(basename "$relpath")"
  # ... existing normalization logic ...
done
```

#### 4. `.github/workflows/ingest.yml` (excerpt)
```yaml
# .github/workflows/ingest.yml
jobs:
  generate-snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.upload.outputs.snapshot-path }}
    steps:
      - uses: actions/checkout@v4
      - name: Generate snapshot
        run: |
          python3 -m pip install huggingface_hub jq
          bin/snapshot.sh --output snapshot.json
      - name: Upload snapshot artifact
        uses: actions/upload-artifact@v4
        id: upload
        with:
          name: snapshot
          path: snapshot.json

  ingest-shard:
    needs: generate-snapshot
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Download snapshot
        uses: actions/download-artifact@v4
        with:
          name: snapshot
      - name: Run shard
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          chmod +x bin/dataset
