# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single Mac-side script that lists one date folder via `list_repo_tree(recursive=False)` and writes `file-list.json`. Embeds into training scripts so Lightning workers do CDN-only fetches with zero API calls during data load.
2. **`bin/dataset-enrich.sh`** — updated to accept optional `FILE_LIST` path; if provided, iterates local JSON instead of calling `list_repo_*` (avoids 429s during parallel shard runs). Falls back to current behavior if absent.
3. **`requirements.txt`** — add `requests` for robust CDN downloads with retries/backoff.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a single date folder.
Run from Mac (or any dev machine) after rate-limit window clears.

Usage:
  python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-04-30 \
    --out file-list.json

Output format:
{
  "repo": "...",
  "date": "...",
  "files": [
    {"path": "batches/public-raw/2026-04-30/foo.parquet", "size": 12345},
    ...
  ],
  "generated_at": "2026-04-30T12:34:56Z"
}
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

CDN_BASE = "https://huggingface.co/datasets"

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="List HF dataset folder (non-recursive).")
    p.add_argument("--repo", required=True, help="HF dataset repo (user/name)")
    p.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-30")
    p.add_argument("--out", default="file-list.json", help="Output JSON path")
    p.add_argument("--prefix", help="Optional custom prefix (overrides date)")
    return p

def list_folder(repo: str, date: str, prefix: str | None = None) -> list[dict]:
    api = HfApi()
    folder = prefix or f"batches/public-raw/{date}"
    # recursive=False avoids paginating 100x and hitting 429
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        files.append({
            "path": entry.path,
            "size": entry.size or 0,
            "cdn_url": f"{CDN_BASE}/{repo}/resolve/main/{entry.path}"
        })
    # Deterministic ordering for stable shard assignment
    files.sort(key=lambda x: x["path"])
    return files

def main() -> None:
    args = build_parser().parse_args()
    try:
        files = list_folder(args.repo, args.date, args.prefix)
    except Exception as exc:
        print(f"ERROR listing folder: {exc}", file=sys.stderr)
        sys.exit(1)

    payload = {
        "repo": args.repo,
        "date": args.date,
        "prefix": args.prefix,
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Updated: prefer local FILE_LIST to avoid HF API 429 during parallel shard runs.
# If FILE_LIST is provided, iterate local JSON; otherwise fall back to live listing.

set -euo pipefail
SHELL=/bin/bash

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:?required}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
FILE_LIST="${FILE_LIST:-}"   # optional: path to JSON from bin/list_files.py
OUT_DIR="${OUT_DIR:-batches/public-merged/${DATE}}"
TIMESTAMP="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "$OUT_FILE")"

log() { echo "[$(date -Iseconds)] $*"; }

# Deterministic shard assignment by slug-hash
shard_for() {
  local slug="$1"
  # 0..65535 then mod TOTAL_SHARDS
  local hash
  hash=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( hash % TOTAL_SHARDS ))
}

process_file() {
  local path="$1"
  local cdn_url="$2"
  # Project to {prompt,response} only at parse time (schema-agnostic).
  # Keep pyarrow happy by avoiding mixed schemas in a single load_dataset call.
  python3 -c "
import json, sys, pyarrow.parquet as pq, io, urllib.request
try:
    with urllib.request.urlopen('$cdn_url') as resp:
        tbl = pq.read_table(io.BytesIO(resp.read()))
    for rec in tbl.to_pylist():
        prompt = rec.get('prompt') or rec.get('input') or ''
        response = rec.get('response') or rec.get('output') or ''
        if prompt or response:
            print(json.dumps({'prompt': prompt, 'response': response}))
except Exception as e:
    sys.stderr.write(f'WARN: {e} | {sys.argv[1]}\\n')
" "$path" 2>/dev/null | while IFS= read -r line; do
    [ -z "$line" ] && continue
    slug="$(echo "$line" | python3 -c "import sys,hashlib;print(hashlib.md5(sys.stdin.read().encode()).hexdigest())")"
    target_shard="$(shard_for "$slug")"
    if [ "$target_shard" -eq "$SHARD_ID" ]; then
      echo "$line"
    fi
  done
}

main() {
  log "Starting shard ${SHARD_ID}/${TOTAL_SHARDS} for ${DATE}"

  if [ -n "$FILE_LIST" ] && [ -f "$FILE_LIST" ]; then
    log "Using local file list: $FILE_LIST"
    mapfile -t ENTRIES < <(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data=json.load(f)
for f in data.get('files', []):
    print(f['path'] + '|' + f.get('cdn_url',''))
" "$FILE_LIST")
  else
    log "No FILE_LIST provided; falling back to live repo listing (may hit API limits)"
    mapfile -t ENTRIES < <(python3 -c "
from huggingface_hub import HfApi
api=HfApi()
folder='batches/public-raw/${DATE}'
for e in api.list_repo_tree(repo='${REPO}', path=folder, recursive=False):
    if e.type=='file':
        print(e.path + '|' + 'https://huggingface.co/datasets/${REPO}/resolve/main/' + e.path)
" 2>/dev/null || true)
  fi

  total=${#ENTRIES[@]}
  log "Found $total files to process"

  count=0
  for entry in "${ENTRIES[@]}"; do
    IFS='|' read -r path cdn_url <<< "$entry"
    [ -z "$
