# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single-call tree walker that saves `file-list.json` for a date folder (recursive=False per folder to avoid 100× pagination).  
   - Uses HF Hub `list_repo_tree` once per subfolder, flattens paths.  
   - Emits `{"date":"YYYY-MM-DD","files":["path1.parquet",...],"sha256":"..."}` so training can pin exact snapshot.  
   - Mac/orchestrator runs this after rate-limit window clears; Lightning training uses only CDN URLs from the list (zero API calls during data load).

2. **`bin/dataset-enrich.sh`** — updated worker to accept `FILE_LIST` (JSON) and stream via CDN URLs.  
   - Falls back to `load_dataset(..., streaming=True)` only if list missing (back-compat).  
   - Projects to `{prompt,response}` at parse time; writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.  
   - Adds retry/back-off on CDN 429 (CDN tier rarely 429s, but safe).

3. **`requirements.txt`** — add `requests` if not present (CDN fetch) and pin `huggingface_hub>=0.22` for `list_repo_tree`.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list + content hash for a date folder in
axentx/surrogate-1-training-pairs.

Usage (Mac orchestration):
  python3 bin/list_files.py --date 2026-05-02 --out file-list.json

Outputs JSON:
{
  "repo": "axentx/surrogate-1-training-pairs",
  "date": "2026-05-02",
  "files": [
    "batches/public-merged/2026-05-02/part-00000.parquet",
    ...
  ],
  "sha256": "e3b0c442...",
  "cdn_base": "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"
}
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

try:
    from huggingface_hub import HfApi
except ImportError:
    print("error: huggingface_hub not installed", file=sys.stderr)
    sys.exit(1)

API = HfApi()
REPO = "axentx/surrogate-1-training-pairs"

def list_date_folder(date_str: str):
    folder_path = f"batches/public-merged/{date_str}"
    try:
        items = API.list_repo_tree(repo_id=REPO, path=folder_path, recursive=False)
    except Exception as exc:
        print(f"error listing {folder_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for item in items:
        if getattr(item, "type", None) == "file" or (hasattr(item, "path") and item.path):
            files.append(item.path)

    # Deterministic ordering so all shards see same snapshot
    files.sort()
    return files

def main():
    parser = argparse.ArgumentParser(description="Generate CDN file list + hash for date folder.")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under batches/public-merged/")
    parser.add_argument("--out", default="file-list.json", help="Output JSON path")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("error: --date must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    files = list_date_folder(args.date)
    payload_bytes = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    sha256 = hashlib.sha256(payload_bytes).hexdigest()

    payload = {
        "repo": REPO,
        "date": args.date,
        "files": files,
        "sha256": sha256,
        "cdn_base": f"https://huggingface.co/datasets/{REPO}/resolve/main",
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

    print(f"wrote {len(files)} files -> {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh`

Key changes:
- Accept optional `FILE_LIST` (JSON). If present, use CDN-only ingestion (bypass HF API).
- Keep existing `load_dataset(..., streaming=True)` fallback when no list provided (local dev / ad-hoc).
- Projects to `{prompt,response}` at parse time; writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
- Adds retry/back-off on CDN 429 (CDN tier rarely 429s, but safe).

```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env:
#   HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
#   SHARD_ID          - 0..15
#   SHARD_TOTAL       - default 16
# Optional env:
#   FILE_LIST         - if set, use CDN-only ingestion from this JSON snapshot
#   DATE_FOLDER       - e.g. 2026-05-02 (defaults to today)

REPO_DST="axentx/surrogate-1-training-pairs"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
SHARD_ID="${SHARD_ID:-0}"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
OUT_PREFIX="batches/public-merged/${DATE_FOLDER}/shard${SHARD_ID}"
TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_PREFIX}-${TIMESTAMP}.jsonl"
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

echo "[$(date -u -Iseconds)] shard ${SHARD_ID}/${SHARD_TOTAL} -> ${OUT_FILE}"

# Helper: deterministic shard assignment by filename hash
shard_for_path() {
  local path="$1"
  # Stable numeric hash; ensure positive
  local hash
  hash=$(echo -n "$path" | cksum | awk '{print $1}')
  echo $(( hash % SHARD_TOTAL ))
}

# Retry/back-off helper for CDN fetches
retry_curl() {
  local url="$1"
  local out="$2"
  local max_retries=5
  local attempt=0
  local code=0
  while (( attempt < max_retries )); do
    if curl -f -sS --retry 2 --retry-delay 1 --retry-max-time 10 -o "$out" "$url"; then
      return 0
    fi
    code=$?
    attempt=$(( attempt + 1 ))
    sleep $(( 1 << attempt ))
  done
  echo "error: failed to fetch $url after $max_retries attempts (code $code)" >&2
  return $code
}

# If FILE_LIST provided, use CDN-only ingestion (avoids HF API 429)
if [[ -n "${FILE_LIST:-}" && -f "$FILE_LIST" ]]; then
  echo "[$(date -u -Iseconds)] using CDN-only ingestion from ${FILE_LIST}"
  CDN_BASE=$(jq -r '.cdn_base' "$FILE_LIST")
  mapfile -t ALL_PATHS < <(jq -r '.files[]' "$FILE_LIST")

  # Filter to this shard
  declare -a MY_PATHS=()
  for p in "${ALL_PATHS[@]}"; do
    if [[ $(shard_for_path "$p") -eq "$SHARD_ID" ]]; then
      MY_PATHS+=("$p")
    fi
  done
