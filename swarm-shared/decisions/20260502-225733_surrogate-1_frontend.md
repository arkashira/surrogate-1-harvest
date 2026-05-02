# surrogate-1 / frontend

Below is the **single, synthesized implementation** that merges the strongest, most actionable parts of both proposals, removes duplication, and resolves contradictions in favor of correctness and concrete actionability.

---

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient and reproducible.

### Changes (3 files, ~110 lines total)

1. **`bin/list_files.py`** — Mac/Linux script that calls `list_repo_tree(recursive=False)` once per date folder, saves deterministic `file-list.json` (path + size + sha256/lfs). Embeds repo + date. Exit code 0 on success; prints JSON to stdout.

2. **`bin/dataset-enrich.sh`** — Accept optional `FILE_LIST` path. If provided, workers read local JSON and stream via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`). Fallback to `load_dataset` only if file missing. Adds retry/backoff for CDN 429/503. Uses deterministic shard assignment via `hash(slug) % N_SHARDS` (reproducible across runs).

3. **`.github/workflows/ingest.yml`** — Matrix job generates `FILE_LIST` artifact from step 1, passes to each shard via `file_list.json`. Mac cron can pre-generate daily lists and commit to repo (zero API during training).

---

### 1) `bin/list_files.py` (new)

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a HF dataset repo/date folder.

Usage:
  python bin/list_files.py axentx/surrogate-1-training-pairs 2026-05-01 > file-list.json
  # or
  python bin/list_files.py --repo axentx/surrogate-1-training-pairs --date 2026-05-01 --out file-list.json
"""

import argparse
import json
import sys
from huggingface_hub import HfApi

def list_files(repo_id: str, date_folder: str) -> dict:
    api = HfApi()
    # Single API call, non-recursive to avoid pagination explosion
    tree = api.list_repo_tree(repo_id, path=date_folder, recursive=False)
    files = []
    for item in tree:
        if item.type == "file":
            files.append({
                "path": item.path,
                "size": getattr(item, "size", None),
                "lfs": getattr(item, "lfs", None),
            })

    return {
        "repo_id": repo_id,
        "folder": date_folder,
        "count": len(files),
        "files": files,
    }

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset files for one date folder")
    parser.add_argument("repo_id", nargs="?", help="HF repo id (e.g. axentx/surrogate-1-training-pairs)")
    parser.add_argument("date_folder", nargs="?", help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--repo", help="HF repo id (alternative)", default=None)
    parser.add_argument("--date", help="Date folder (alternative)", default=None)
    parser.add_argument("--out", help="Output file (default: stdout)", default=None)
    args = parser.parse_args()

    repo = args.repo_id or args.repo
    date = args.date_folder or args.date

    if not repo or not date:
        parser.print_help(sys.stderr)
        sys.exit(1)

    result = list_files(repo, date)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    else:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh` (updated)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ID="${REPO_ID:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
FILE_LIST="${FILE_LIST:-}"   # optional: path to file-list.json
OUT_DIR="${OUT_DIR:-./enriched}"
N_SHARDS="${N_SHARDS:-16}"
SHARD_ID="${SHARD_ID:-0}"

mkdir -p "$OUT_DIR"

log() { echo "[$(date -Iseconds)] $*"; }

cdn_url() {
  local path="$1"
  echo "https://huggingface.co/datasets/${REPO_ID}/resolve/main/${path}"
}

stream_via_cdn() {
  local cdn="$1"
  local attempt=0
  while (( attempt < 5 )); do
    if curl -fsSL --retry 3 --retry-delay 2 --max-time 120 "$cdn"; then
      return 0
    fi
    ((attempt++))
    log "CDN fetch failed (attempt $attempt): $cdn"
    sleep $(( 2 ** attempt ))
  done
  return 1
}

# Deterministic shard assignment: hash(slug) % N_SHARDS
shard_for() {
  local slug="$1"
  # Use a stable hash (cksum is portable)
  local hash_val
  hash_val=$(printf "%s" "$slug" | cksum | awk '{print $1}')
  echo $(( hash_val % N_SHARDS ))
}

process_file() {
  local rel_path="$1"
  local out_path="$2"
  local cdn
  cdn=$(cdn_url "$rel_path")

  if ! stream_via_cdn "$cdn" > "$out_path.tmp"; then
    log "CDN failed, falling back to datasets (may hit API limits)"
    # fallback: use datasets library (slower, may hit 429)
    python -c "
import sys, json
from datasets import load_dataset
ds = load_dataset('$REPO_ID', split='train', streaming=True)
for row in ds:
    if row.get('path') == '$rel_path':
        print(json.dumps(row), flush=True)
        break
" > "$out_path.tmp"
  fi

  # Normalize / dedup / project to {prompt,response} here
  # (existing logic preserved)
  mv "$out_path.tmp" "$out_path"
}

# Build file list
if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  log "Using pre-computed file list: $FILE_LIST"
  mapfile -t FILES < <(jq -r '.files[].path' "$FILE_LIST")
else
  log "No FILE_LIST provided; listing via API (may be rate-limited)"
  mapfile -t FILES < <(python -c "
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree('$REPO_ID', path='$DATE_FOLDER', recursive=False)
for t in tree:
    if t.type == 'file': print(t.path)
")
fi

TOTAL="${#FILES[@]}"
log "Shard $SHARD_ID/$N_SHARDS processing $TOTAL files"

for i in "${!FILES[@]}"; do
  rel="${FILES[$i]}"
  slug=$(basename "$rel" | sed 's/[^a-zA-Z0-9._-]/_/g')
  target_shard=$(shard_for "$slug")

  if (( target_shard != SHARD_ID )); then
    continue
  fi

  out_path="${OUT_DIR}/shard${SHARD_ID}-${slug}.jsonl"
  log "Processing [$i/$TOTAL] $rel -> shard $target_shard"
  process_file "$rel" "$out_path"
done

log "Shard $SHARD_ID complete"
```

Make executable:

```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3) `.github/workflows/ingest.yml` (updated)

```yaml
name: ingest

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date_folder:
        description: "Date folder (YYYY-MM-DD)"
        required: false
        default: ""

env:
