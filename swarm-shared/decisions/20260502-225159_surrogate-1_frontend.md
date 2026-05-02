# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — one-time API call from Mac (or cron) to list a date folder, save to JSON. Embeddable by training scripts and shard workers so they never call `list_repo_tree`/`load_dataset` during ingestion/training.
2. **`bin/dataset-enrich.sh`** — accept optional file-list JSON; if provided, iterate local paths and fetch via CDN (`/resolve/main/...`) with `curl`/`wget` (zero API auth). Fallback to HF datasets only if no list.
3. **`lib/dedup.py`** — no change (already central md5 store).

### Why this matters
- Eliminates `list_repo_tree`/`load_dataset` API calls during ingestion (workers use CDN URLs, no auth/rate-limit).
- Single Mac-side listing fits in rate-limit window; workers become read-only CDN clients.
- Keeps current 16-shard parallelism and dedup behavior; no infra changes.
- <2h: small Python script + shell tweak + tests.

---

## 1/3 `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
List files for a single date folder in axentx/surrogate-1-training-pairs
and emit a JSON file with CDN URLs for CDN-only ingestion.

Usage:
  python3 bin/list_files.py --date 2026-04-29 --out file_list.json

Notes:
- Uses list_repo_tree(path, recursive=False) per subfolder to avoid
  recursive pagination (100× limit risk).
- CDN URLs are public and bypass HF API auth/rate limits.
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_files(date_str: str):
    """
    Return list of dict:
      {"path": "...", "cdn_url": "...", "size": int|None}
    for files under snapshots/{date_str}/ and public-merged/{date_str}/
    """
    api = HfApi()
    date_str = date_str.strip("/")
    prefixes = [
        f"snapshots/{date_str}",
        f"public-merged/{date_str}",
        f"batches/public-merged/{date_str}",
    ]

    results = []
    seen = set()

    for prefix in prefixes:
        try:
            items = api.list_repo_tree(REPO_ID, path=prefix, recursive=False)
        except Exception as exc:
            # Path may not exist; skip silently
            print(f"Warning: could not list {prefix}: {exc}", file=sys.stderr)
            continue

        for item in items:
            # list_repo_tree may return nested tree objects; we only want files
            if getattr(item, "type", None) == "directory" or getattr(item, "size", None) is None:
                continue
            path = item.rfilename if hasattr(item, "rfilename") else str(item)
            if not path or path in seen:
                continue
            seen.add(path)
            cdn_url = CDN_TEMPLATE.format(repo=REPO_ID, path=path)
            results.append({
                "path": path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
            })

    # Deterministic ordering for reproducible sharding
    results.sort(key=lambda x: x["path"])
    return results

def main():
    parser = argparse.ArgumentParser(description="List date folder files for CDN ingestion")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="file_list.json", help="Output JSON path")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("Error: --date must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    files = list_date_files(args.date)
    payload = {
        "date": args.date,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "repo": REPO_ID,
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) if os.path.dirname(args.out) else ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

## 2/3 `bin/dataset-enrich.sh` (updated)

Key changes:
- Accept optional `FILE_LIST_JSON` environment variable (or arg).  
- If provided, iterate CDN URLs directly with `curl` + streaming JSONL parsing (avoids `datasets.load_dataset` recursive listing and auth/429).  
- Keep existing schema normalization and dedup behavior.  
- Fallback to original behavior if no list provided.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Worker script for surrogate-1-runner ingestion.
#
# Usage (GitHub Actions):
#   env:
#     SHARD_ID: 0..15
#     HF_TOKEN: write token
#     FILE_LIST_JSON: optional path to file_list.json produced by list_files.py
#
# If FILE_LIST_JSON is provided, workers will use CDN-only fetching and skip
# datasets.load_dataset recursive listing (avoids HF API 429).

set -euo pipefail
SHELL=/bin/bash

cd /opt/axentx/surrogate-1-runner || { echo "repo root not found"; exit 1; }

SHARD_ID="${SHARD_ID:-0}"
HF_TOKEN="${HF_TOKEN:-}"
FILE_LIST_JSON="${FILE_LIST_JSON:-}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE_FOLDER="${DATE_FOLDER:-$(date -u +%Y-%m-%d)}"
OUT_DIR="batches/public-merged/${DATE_FOLDER}"
TIMESTAMP=$(date -u +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "${OUT_FILE}")"

echo "[$(date -u)] Shard ${SHARD_ID}/${TOTAL_SHARDS} starting (date=${DATE_FOLDER})"

# Dedup helper
python3 -c "import sys; sys.path.insert(0, 'lib'); from dedup import DedupStore; d=DedupStore(); print('DedupStore OK')" || {
  echo "DedupStore import failed"
  exit 1
}

# Helper: deterministic shard assignment by filename hash
shard_for_path() {
  local path="$1"
  # Stable numeric hash (FNV-1a-ish via cksum) modulo TOTAL_SHARDS
  local hash
  hash=$(echo -n "$path" | cksum | awk '{print $1}')
  echo $(( hash % TOTAL_SHARDS ))
}

# Normalize a single JSONL record into {prompt, response}
# Extend as needed per schema.
normalize_record() {
  python3 -c "
import sys, json
rec = json.load(sys.stdin)
prompt = rec.get('prompt') or rec.get('input') or rec.get('question') or ''
response = rec.get('response') or rec.get('output') or rec.get('answer') or ''
if not isinstance(prompt, str): prompt = json.dumps(prompt)
if not isinstance(response, str): response = json.dumps(response)
out = {'prompt': prompt, 'response': response}
json.dump(out, sys.stdout)
"
}

# Process a single CDN URL
process_cdn_file() {
  local url="$1"
  # Stream download and parse line-by-line (assumes JSONL)
  curl -fsSL "$url" | while IFS= read -r line; do
    [ -z "${line}" ] && continue
    local normalized
    normalized=$(echo "$line" | normalize_record) || continue
    local shard
   
