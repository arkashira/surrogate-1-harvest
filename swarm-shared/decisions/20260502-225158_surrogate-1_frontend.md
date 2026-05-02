# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — Mac-side script to list top-level date folders once (post-rate-limit window), save `file_list.json`. Embeds into repo so training/shard workers use CDN-only paths.
2. **`bin/dataset-enrich.sh`** — Updated to accept an optional file-list JSON; if provided, workers iterate CDN URLs directly instead of calling `load_dataset`/`list_repo_files` repeatedly.
3. **`.github/workflows/ingest.yml`** — Pass the file-list artifact to matrix shards; fallback to legacy behavior if absent.

---

## 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Usage (Mac, after rate-limit clears):
  python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --out file_list.json

Produces:
  {
    "repo": "...",
    "generated_at": "...",
    "folders": {
      "2026-04-29": ["file1.parquet", ...],
      ...
    },
    "cdn_base": "https://huggingface.co/datasets/{repo}/resolve/main"
  }
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

CDN_TMPL = "https://huggingface.co/datasets/{repo}/resolve/main"

def list_date_folders(api: HfApi, repo: str):
    # Non-recursive top-level only (fast, 1 page)
    items = api.list_repo_tree(repo=repo, path="", recursive=False)
    folders = {}
    for item in items:
        if item.type == "directory":
            name = item.path.rstrip("/")
            # Expect YYYY-MM-DD; skip others
            if len(name) == 10 and name[4] == "-" and name[7] == "-":
                sub = api.list_repo_tree(repo=repo, path=name, recursive=False)
                files = [it.path for it in sub if it.type == "file"]
                folders[name] = sorted(files)
    return folders

def main():
    parser = argparse.ArgumentParser(description="List dataset files for CDN-only ingestion.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/name)")
    parser.add_argument("--out", default="file_list.json", help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    try:
        folders = list_date_folders(api, args.repo)
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    payload = {
        "repo": args.repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "folders": folders,
        "cdn_base": CDN_TMPL.format(repo=args.repo),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

    print(f"Wrote {len(folders)} date folders to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

## 2) `bin/dataset-enrich.sh`

Update worker to optionally use CDN file list (avoids repeated HF API calls).

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Usage:
#   ./dataset-enrich.sh [--file-list FILE_LIST_JSON]
#
# Environment:
#   HF_TOKEN         required for upload
#   SHARD_ID         0..15 (from matrix)
#   TOTAL_SHARDS     16

set -euo pipefail
SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +"%Y-%m-%d")
TS=$(date -u +"%H%M%S")
OUT="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"

FILE_LIST=""
if [[ "${1:-}" == "--file-list" ]]; then
  FILE_LIST="${2:-}"
  shift 2
fi

mkdir -p "$(dirname "$OUT")"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

# Deterministic shard assignment by slug-hash
shard_for() {
  local slug="$1"
  # Fast deterministic 0..15
  python3 -c "import hashlib; print(int(hashlib.md5('$slug'.encode()).hexdigest(), 16) % $TOTAL_SHARDS)"
}

process_file_cdn() {
  local cdn_url="$1"
  local slug="$2"
  # Lightweight streaming: download only this file, project to {prompt,response}
  # Replace with your schema-specific projection logic.
  python3 -c "
import sys, json, pyarrow.parquet as pq, urllib.request, tempfile, os, hashlib
url = sys.argv[1]
slug = sys.argv[2]
try:
  with tempfile.NamedTemporaryFile(suffix='.parquet', delete=False) as tf:
    tfpath = tf.name
    urllib.request.urlretrieve(url, tfpath)
  table = pq.read_table(tfpath, columns=['prompt','response'])
  os.unlink(tfpath)
  for batch in table.to_batches():
    cols = batch.to_pydict()
    for i in range(len(cols['prompt'])):
      obj = {'prompt': cols['prompt'][i], 'response': cols['response'][i], 'slug': slug}
      print(json.dumps(obj, ensure_ascii=False))
except Exception as e:
  sys.stderr.write(f'WARN {slug}: {e}\\n')
" "$cdn_url" "$slug"
}

process_legacy() {
  # Fallback: use datasets library (may hit API limits)
  python3 -c "
import sys, json
from datasets import load_dataset
repo = sys.argv[1]
slug = sys.argv[2]
try:
  ds = load_dataset(repo, name=None, streaming=True, split='train')
  for item in ds:
    obj = {'prompt': item.get('prompt'), 'response': item.get('response'), 'slug': slug}
    print(json.dumps(obj, ensure_ascii=False))
except Exception as e:
  sys.stderr.write(f'WARN {slug}: {e}\\n')
" "$REPO" "$slug"
}

main() {
  log "Starting shard ${SHARD_ID}/${TOTAL_SHARDS} for ${DATE}"

  if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
    log "Using CDN file list: $FILE_LIST"
    cdn_base=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['cdn_base'])") "$FILE_LIST"
    # Iterate folders/files deterministically
    python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for folder, files in sorted(data['folders'].items()):
  for f in sorted(files):
    slug = f.replace('/', '_').replace('.parquet', '')
    shard = $(shard_for "$slug")
    if int(shard) == ${SHARD_ID}:
      print(f'{folder}/{f}')
" "$FILE_LIST" | while IFS= read -r relpath; do
      slug=$(basename "$relpath" .parquet | tr "/" "_")
      url="${cdn_base}/${relpath}"
      process_file_cdn "$url" "$slug"
    done
  else
    log "No file list provided — using legacy mode (may hit HF API limits)"
    # Minimal legacy behavior: list repo files once per shard (still API calls)
    python3 -c "
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree('$REPO', recursive=True)
for it in items:
  if it.type == 'file' and it.path.endswith('.parquet'):
    print(it.path)
" | while IFS= read -r relpath; do
      slug=$(basename "$relpath" .parquet | tr "/"
