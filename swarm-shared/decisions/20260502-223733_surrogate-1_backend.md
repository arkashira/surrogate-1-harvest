# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-time Mac/CI script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path + size + sha256). Embed this list in training/shard scripts so Lightning workers do **zero API calls** during data load (CDN-only).
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON. If provided, workers stream only listed files via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`). If not, fall back to current behavior.
3. Add `bin/train-cdn.sh` launcher for Lightning Studio that passes the file-list, reuses running studios, and uses L40S (or H200 in `lightning-lambda-prod` when available).

### Why this matters
- HF API 429 (1000 req/5m) blocks `load_dataset` and `list_repo_files` on big repos.
- CDN downloads (`/resolve/main/`) are **not** counted against API limits and have much higher throughput.
- Pre-listing once per date folder removes per-epoch API churn and makes shard retries deterministic.

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  HF_TOKEN=<token> python bin/list-files.py --repo axentx/surrogate-1-training-pairs \
    --folder batches/public-merged/2026-05-02 --out file-list.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def sha256_of_repo_file(api, repo_id, path):
    try:
        info = api.repo_info(repo_id=repo_id, repo_type="dataset", files_metadata=True)
        for f in info:
            if f.path == path and getattr(f, "sha256", None):
                return f.sha256
    except Exception:
        pass
    return None

def main() -> None:
    parser = argparse.ArgumentParser(description="List repo folder for CDN-only ingestion")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--folder", required=True, help="Folder path in repo (e.g. batches/public-merged/2026-05-02)")
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)

    # Use recursive=False per folder to avoid 100x pagination on big repos.
    entries = api.list_repo_tree(repo_id=args.repo, path=args.folder, recursive=False)
    files = [e for e in entries if e.type == "file"]

    # Sort for deterministic ordering across runs.
    files.sort(key=lambda f: f.path)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": args.repo,
        "folder": args.folder,
        "strategy": "cdn-only",
        "files": [
            {
                "path": f.path,
                "size": getattr(f, "size", None),
                "sha256": sha256_of_repo_file(api, args.repo, f.path) or "",
            }
            for f in files
        ],
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) Update `bin/dataset-enrich.sh` (CDN-aware)

Add optional file-list mode and CDN download fallback. Keep existing HF dataset streaming as fallback.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: CDN-only mode via --file-list file-list.json
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:-}"
OUT_DIR="${OUT_DIR:-enriched}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"

mkdir -p "$OUT_DIR"

# Dedup store (central sqlite) - unchanged
DEDUP_DB="${DEDUP_DB:-/tmp/dedup.db}"
python3 -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)')
conn.commit()
conn.close()
" "$DEDUP_DB"

# Deterministic shard assignment by slug hash
shard_of() {
  local slug="$1"
  python3 -c "print(abs(hash('$slug')) % $TOTAL_SHARDS)" 2>/dev/null || echo 0
}

# Download via CDN (no auth header required for public datasets)
cdn_download() {
  local repo="$1"
  local path="$2"
  local out="$3"
  curl -L -s -f --retry 3 --retry-delay 2 -o "$out" "https://huggingface.co/datasets/${repo}/resolve/main/${path}"
}

process_file() {
  local src_path="$1"
  local tmp_jsonl=$(mktemp)

  # If FILE_LIST provided, use CDN; else fallback to datasets streaming
  if [[ -n "$FILE_LIST" ]]; then
    if ! cdn_download "$REPO" "$src_path" "$tmp_jsonl"; then
      echo "WARN: CDN download failed for $src_path, skipping"
      rm -f "$tmp_jsonl"
      return 0
    fi
  else
    # Legacy: stream via datasets (may hit 429)
    python3 -c "
import sys, json
from datasets import load_dataset
repo='$REPO'
path='$src_path'
try:
  ds = load_dataset('json', data_files={'data': f'repo://{repo}/{path}'}, streaming=True)
  for row in ds['data']:
    print(json.dumps(row, ensure_ascii=False))
except Exception as e:
    sys.stderr.write(f'ERROR streaming {path}: {e}\\n')
    sys.exit(1)
" > "$tmp_jsonl" || { echo "WARN: stream failed for $src_path"; rm -f "$tmp_jsonl"; return 0; }
  fi

  # Project to {prompt,response} and dedup by md5
  python3 -c "
import json, hashlib, sqlite3, sys, os
db_path = '$DEDUP_DB'
shard_id = int('$SHARD_ID')
total_shards = int('$TOTAL_SHARDS')
out_dir = '$OUT_DIR'

conn = sqlite3.connect(db_path)
today = __import__('datetime').date.today().isoformat()
batch_dir = f'{out_dir}/batches/public-merged/{today}'
os.makedirs(batch_dir, exist_ok=True)
batch_file = f'{batch_dir}/shard{shard_id}-{__import__(\"datetime\").datetime.now().strftime(\"%H%M%S\")}.jsonl'

def shard_of(slug):
    return abs(hash(slug)) % total_shards

with open(sys.argv[1], 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get('prompt') or obj.get('input') or obj.get('text') or ''
        response = obj.get('response') or obj.get('output') or ''
        if not prompt or not response:
            continue
        slug = obj.get('slug') or hashlib.md5((prompt + response).encode()).hexdigest()[:12]
        if
