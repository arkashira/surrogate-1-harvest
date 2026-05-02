# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single-call tree lister + JSON export  
   - Uses `list_repo_tree(path, recursive=False)` per folder (avoids 100× pagination).  
   - Emits `file_list.json` with `{"date": "...", "path": "...", "size": ...}` for one date folder.  
   - Saves to repo root so workflows can `upload-artifact` and pass to training.

2. **`bin/dataset-enrich.sh`** — switch to CDN-only ingestion  
   - Accepts optional `FILE_LIST` (JSON) or falls back to current behavior.  
   - When `FILE_LIST` is provided, workers stream from `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with no Authorization header (bypasses `/api/` rate limits).  
   - Keeps deterministic shard assignment via `slug-hash % 16 == SHARD_ID`.  
   - Projects to `{prompt, response}` only before writing; attribution moved to filename pattern `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

3. **`.github/workflows/ingest.yml`** — orchestrate list → distribute → train  
   - Adds a `list-files` job that runs once per workflow (not per shard) and uploads `file_list.json` as artifact.  
   - Shard jobs download artifact and pass path via env.  
   - Adds optional `training` job (or docs) showing how to embed the list in Lightning training for CDN-only fetches.

---

### Code snippets

#### 1) `bin/list_files.py`
```python
#!/usr/bin/env python3
"""
List files for one date folder in axentx/surrogate-1-training-pairs
without recursive pagination.

Usage:
  python bin/list_files.py --date 2026-05-02 --out file_list.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

API = HfApi()
REPO = "datasets/axentx/surrogate-1-training-pairs"

def list_date_folder(date: str):
    # Expect folder layout: batches/public-raw/{date}/...
    prefix = f"batches/public-raw/{date}/"
    entries = []
    cursor = None
    while True:
        result = API.list_repo_tree(
            repo_id=REPO,
            path=prefix,
            recursive=False,
            cursor=cursor,
        )
        for item in result:
            # item.path is full path; skip subfolders for now (we only want files)
            if item.type == "file":
                entries.append({
                    "date": date,
                    "path": item.path,
                    "size": getattr(item, "size", None),
                })
        cursor = getattr(result, "cursor", None)
        if not cursor:
            break
    return entries

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", default="file_list.json")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        sys.exit("date must be YYYY-MM-DD")

    entries = list_date_folder(args.date)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)

    print(f"Wrote {len(entries)} files to {args.out}")

if __name__ == "__main__":
    main()
```

#### 2) `bin/dataset-enrich.sh` (updated)
```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic shard worker with optional CDN-only ingestion.
#
# Required env:
#   HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
#   SHARD_ID          - 0..15
#   DATE              - YYYY-MM-DD (e.g. 2026-05-02)
# Optional env:
#   FILE_LIST         - path to JSON from bin/list_files.py
#   OUTDIR            - default: /tmp/enrich_out

set -euo pipefail
export SHELL=/bin/bash

REPO="datasets/axentx/surrogate-1-training-pairs"
OUTDIR="${OUTDIR:-/tmp/enrich_out}"
DATE="${DATE:?DATE required}"
SHARD_ID="${SHARD_ID:?SHARD_ID required (0-15)}"
HF_TOKEN="${HF_TOKEN:?HF_TOKEN required}"

mkdir -p "$OUTDIR/batches/public-merged/$DATE"
OUTFILE="$OUTDIR/batches/public-merged/$DATE/shard${SHARD_ID}-$(date +%H%M%S).jsonl"

# Central dedup store (SQLite) - reused across runs
DEDUP_DB="${DEDUP_DB:-$OUTDIR/dedup.db}"
python3 -c "
import sqlite3, os, sys
db = os.environ['DEDUP_DB']
os.makedirs(os.path.dirname(db), exist_ok=True)
conn = sqlite3.connect(db)
conn.execute('CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)')
conn.commit()
conn.close()
"

# Helper: deterministic shard assignment by slug hash
shard_for() {
  local slug="$1"
  # numeric hash stable across runs
  local h
  h=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( h % 16 ))
}

# Process one file path via CDN (no Authorization header)
process_cdn() {
  local path="$1"
  local url="https://huggingface.co/${REPO}/resolve/main/${path}"
  # stream + project to {prompt,response} only
  python3 -c "
import json, sys, hashlib, sqlite3, os, tempfile, urllib.request

url = sys.argv[1]
db_path = os.environ['DEDUP_DB']
out = sys.argv[2]
shard = int(sys.argv[3])

def extract_pair(raw):
    # Placeholder: adapt per actual schema.
    # Goal: return (prompt_text, response_text) or None.
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    prompt = obj.get('prompt') or obj.get('input') or obj.get('text')
    response = obj.get('response') or obj.get('output') or obj.get('completion')
    if prompt is None or response is None:
        return None
    return str(prompt), str(response)

conn = sqlite3.connect(db_path)
try:
    with urllib.request.urlopen(url) as resp:
        # assume line-delimited JSON; fallback to single JSON array if needed
        data = resp.read().decode('utf-8')
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    written = 0
    with open(out, 'a', encoding='utf-8') as fout:
        for ln in lines:
            pair = extract_pair(ln)
            if pair is None:
                continue
            prompt, response = pair
            slug = hashlib.md5((prompt + response).encode()).hexdigest()
            cur = conn.execute('SELECT 1 FROM seen WHERE md5=?', (slug,))
            if cur.fetchone():
                continue
            if shard_for(slug) != shard:
                continue
            conn.execute('INSERT INTO seen (md5) VALUES (?)', (slug,))
            fout.write(json.dumps({'prompt': prompt, 'response': response}) + '\n')
            written += 1
    conn.commit()
finally:
    conn.close()
" "$url" "$OUTFILE" "$SHARD_ID"
}

# If FILE_LIST provided, use CDN-only deterministic list
if [[ -n "${FILE_LIST:-}" && -f "$FILE_LIST" ]]; then
  echo "Using CDN-only file list: $FILE_LIST"
  python3 -c "
import json, sys, os
with open(sys.argv[1]) as f:
    files = json.load(f)
for item in files:
    print(item['path'])
" "$FILE_LIST" | while IFS= read -r path; do
    process_cdn "$path"
