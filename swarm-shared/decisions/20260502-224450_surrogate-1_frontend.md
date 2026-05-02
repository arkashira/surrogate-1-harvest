# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single API call (after rate-limit window) to `list_repo_tree` per date folder, save JSON of `{path, sha, size}` to `file_manifest.json`. Embeddable by training scripts and shard workers.
2. **`bin/dataset-enrich.sh`** — read `file_manifest.json` if present; stream files via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with `curl --retry 3 --retry-delay 5`. Fallback to HF API only if CDN fails.
3. **`lib/dedup.py`** — expose `get_seen()` and `bulk_mark()` so workers can do batch dedup checks before upload (reduces commit churn and avoids 128/hr cap collisions).

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file manifest for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  python bin/list_files.py --date 2026-05-02 --out file_manifest.json

Embedd this JSON in training/shard scripts to enable CDN-only fetches
and avoid HF API 429 during data loading.
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi, login

REPO_ID = "axentx/surrogate-1-training-pairs"

def build_manifest(date_folder: str, out_path: str) -> None:
    api = HfApi()
    # One API call per date folder (non-recursive) to stay under rate limits.
    items = api.list_repo_tree(
        repo_id=REPO_ID,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )

    manifest = []
    for item in items:
        if item.type != "file":
            continue
        # Only include parquet/jsonl we expect to process.
        if not item.path.lower().endswith((".parquet", ".jsonl")):
            continue
        manifest.append(
            {
                "path": item.path,
                "sha": getattr(item, "sha", None),
                "size": getattr(item, "size", None),
                "cdn_url": (
                    f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{item.path}"
                ),
            }
        )

    manifest.sort(key=lambda x: x["path"])
    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_folder": date_folder,
        "repo": REPO_ID,
        "count": len(manifest),
        "files": manifest,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {len(manifest)} files -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN file manifest.")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional if public)")
    args = parser.parse_args()

    if args.token:
        login(token=args.token)

    try:
        build_manifest(args.date, args.out)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

### 2) `bin/dataset-enrich.sh` (updated worker)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic shard worker with CDN-first ingestion.
#
# Required env:
#   HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
#   SHARD_ID          - 0..15
#   SHARD_TOTAL       - 16
#   DATE_FOLDER       - e.g. 2026-05-02
#   MANIFEST_PATH     - optional path to file_manifest.json (CDN mode)
#
# If MANIFEST_PATH is provided, files are fetched via CDN (no API calls during
# streaming). Falls back to HF datasets library if manifest missing.

set -euo pipefail
SHELL=/bin/bash

cd "$(dirname "$0")/.."

source <(grep = .env 2>/dev/null || true)

HF_TOKEN="${HF_TOKEN:?HF_TOKEN required}"
SHARD_ID="${SHARD_ID:?SHARD_ID required}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
DATE_FOLDER="${DATE_FOLDER:?DATE_FOLDER required}"
MANIFEST_PATH="${MANIFEST_PATH:-}"

export HF_TOKEN
export PYTHONUNBUFFERED=1

WORKDIR=$(mktemp -d)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard $SHARD_ID] $*"
}

# Deterministic shard assignment by slug hash.
# Keeps behavior aligned with README: 1/16 slice.
assign_shard() {
  local slug=$1
  local hash
  # Deterministic, stable across runs.
  hash=$(echo -n "$slug" | md5sum | cut -c1-8)
  echo $((0x$hash % SHARD_TOTAL))
}

process_file_cdn() {
  local cdn_url=$1
  local out=$2
  curl --silent --show-error --retry 3 --retry-delay 5 --max-time 300 \
    -H "Authorization: Bearer ${HF_TOKEN}" \
    "$cdn_url" > "$out"
}

process_file_hf() {
  local repo_path=$1
  local out=$2
  python3 -c "
import os, sys
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='axentx/surrogate-1-training-pairs',
    filename='$repo_path',
    repo_type='dataset',
    local_dir='$WORKDIR/dl',
    token=os.environ['HF_TOKEN'],
)
" 2>/dev/null
  # Move downloaded file to expected location.
  find "$WORKDIR/dl" -name "$(basename "$repo_path")" -exec mv {} "$out" \;
}

run_shard() {
  local manifest_path=$1
  local batch_dir="$WORKDIR/batches"
  mkdir -p "$batch_dir"

  local timestamp
  timestamp=$(date -u +%Y%m%d-%H%M%S)
  local out_name="shard${SHARD_ID}-${timestamp}.jsonl"
  local out_path="$batch_dir/$out_name"
  > "$out_path"

  local processed=0 skipped=0 errors=0

  if [[ -f "$manifest_path" ]]; then
    log "CDN mode: using manifest $manifest_path"
    mapfile -t entries < <(
      python3 -c "
import json, sys
with open('$manifest_path') as f:
    data = json.load(f)
for fobj in data.get('files', []):
    print(fobj['path'] + '|' + fobj.get('cdn_url', ''))
"
    )
  else
    log "Fallback: listing repo tree (single API call)"
    mapfile -t entries < <(
      python3 -c "
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree(
    repo_id='axentx/surrogate-1-training-pairs',
    path='$DATE_FOLDER',
    repo_type='dataset',
    recursive=False,
)
for it in items:
    if it.type == 'file' and it.path.lower().endswith(('.parquet','.jsonl')):
        print(it.path + '|')
"
    )
  fi

  for entry in "${entries[@]}
