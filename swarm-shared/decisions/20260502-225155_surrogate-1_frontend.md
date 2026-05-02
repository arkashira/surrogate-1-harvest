# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — Mac-side script to list date folders once, save JSON for training/shard jobs.  
2. **`bin/dataset-enrich.sh`** — Updated to accept pre-computed file list (JSON) and use CDN URLs exclusively; fallback to HF API only if CDN fails.  
3. **`lib/dedup.py`** — Minor: add `cdn_url` field to dedup record for traceability; no logic change.

### Why this matters
- Eliminates `list_repo_files` recursive calls that trigger 429s.  
- CDN downloads bypass auth rate limits entirely.  
- Training scripts can embed the file list and run zero-API data loads (Lightning quota-safe).  
- Shard workers become deterministic and reproducible per date folder.

---

## 1) `bin/list_files.py` (new)

```python
#!/usr/bin/env python3
"""
List public dataset files for a given date folder (or latest) and emit JSON.
Intended to run once per cron cycle on a Mac (or CI) before shard workers start.

Usage:
  python bin/list_files.py --repo axentx/surrogate-1-training-pairs \
                           --date 2026-05-02 \
                           --out file-list-2026-05-02.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_files(repo: str, date_folder: str, api: HfApi):
    """
    List files under <date_folder>/ recursively (shallow per folder) to avoid
    massive recursive listing. Returns list of dicts with cdn_url and metadata.
    """
    base = date_folder.strip("/")
    try:
        tree = api.list_repo_tree(repo=repo, path=base, recursive=False)
    except Exception as e:
        print(f"HF API error listing {repo}/{base}: {e}", file=sys.stderr)
        return []

    entries = []
    for item in tree:
        if item.type != "file":
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=item.path)
        entries.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
            "lfs": getattr(item, "lfs", None) is not None,
        })
    return entries

def main():
    parser = argparse.ArgumentParser(description="List dataset files for CDN ingestion.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--out", required=True, help="Output JSON file")
    args = parser.parse_args()

    api = HfApi()
    files = list_date_files(args.repo, args.date, api)

    payload = {
        "repo": args.repo,
        "date": args.date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
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

## 2) `bin/dataset-enrich.sh` (updated)

```bash
#!/usr/bin/env bash
#
# dataset-enrich.sh
# Normalize and dedup training pairs for a deterministic file list (JSON).
#
# Usage (shard worker):
#   export SHARD_ID=0
#   export FILE_LIST=file-list-2026-05-02.json
#   bin/dataset-enrich.sh
#
# If FILE_LIST is unset, falls back to HF API listing (slower; may 429).

set -euo pipefail
SHELL=/bin/bash

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
OUT_DIR="${OUT_DIR:-output}"
HF_TOKEN="${HF_TOKEN:-}"

FILE_LIST="${FILE_LIST:-}"
TS="$(date -u +%Y%m%d%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "${OUT_DIR}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Deterministic shard assignment by slug-hash
shard_for() {
  local slug="$1"
  # 0..65535 then modulo N_SHARDS
  local hash
  hash=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( hash % 65536 % N_SHARDS ))
}

# Dedup via central store (imports lib/dedup)
dedup_and_store() {
  local prompt="$1"
  local response="$2"
  local source="$3"
  local cdn_url="$4"
  python3 -c "
import sys, json
from lib.dedup import is_duplicate, store_hash
prompt = sys.argv[1]
response = sys.argv[2]
source = sys.argv[3]
cdn_url = sys.argv[4]
if is_duplicate(prompt, response):
    sys.exit(0)
store_hash(prompt, response)
print(json.dumps({'prompt': prompt, 'response': response, 'source': source, 'cdn_url': cdn_url, 'ts': sys.argv[5]}))
" "$prompt" "$response" "$source" "$cdn_url" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${OUT_FILE}.tmp"
}

process_file_cdn() {
  local cdn_url="$1"
  local source="$2"
  # Stream with curl and parse line-by-line (parquet/jsonl handled per file type)
  # For simplicity, assume JSONL here; extend with 'file' detection as needed.
  curl -fsSL --retry 3 --retry-delay 5 "$cdn_url" | while IFS= read -r line; do
    # Minimal projection: expect {prompt, response} or similar
    local prompt response
    prompt=$(echo "$line" | python3 -c "import sys,json;print(json.loads(sys.stdin.read()).get('prompt',''))" 2>/dev/null || true)
    response=$(echo "$line" | python3 -c "import sys,json;print(json.loads(sys.stdin.read()).get('response',''))" 2>/dev/null || true)
    if [[ -z "$prompt" || -z "$response" ]]; then
      continue
    fi
    local slug
    slug=$(echo -n "${prompt}${response}" | sha256sum | awk '{print $1}')
    local my_shard
    my_shard=$(shard_for "$slug")
    if [[ "$my_shard" -eq "$SHARD_ID" ]]; then
      dedup_and_store "$prompt" "$response" "$source" "$cdn_url"
    fi
  done
}

# Main
log "Starting shard ${SHARD_ID}/${N_SHARDS} for ${DATE}"

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  log "Using pre-computed file list: ${FILE_LIST}"
  mapfile -t files < <(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for fobj in data.get('files', []):
    print(fobj['cdn_url'] + '|' + fobj['path'])
" "$FILE_LIST")
else
  log "FILE_LIST not provided or missing; falling back to HF API (may 4
