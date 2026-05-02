# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-time Mac/CI script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path + size + sha256). Embed this list in training scripts so Lightning workers do **zero** HF API calls during data load (CDN-only).
2. Update `bin/dataset-enrich.sh` to accept an optional file-list; if provided, workers stream only listed files via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) and skip `load_dataset`/`list_repo_files` during the run.
3. Add `bin/train-cdn.sh` wrapper for Lightning Studio that injects the file-list and sets `HF_DATASETS_OFFLINE=1` to prevent accidental API calls.

### Why this matters
- Avoids `list_repo_files` recursive pagination (100× requests) and 429s.
- CDN downloads bypass `/api/` auth checks and have much higher rate limits.
- Single Mac-side API call per date folder (after rate-limit window) → safe.
- Training becomes reproducible and quota-safe (studio/L40S runs without HF API dependencies).

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file-list for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  HF_TOKEN=<token> python bin/list-files.py --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 --out file-list.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

def sha256_of_path(path: str, api: HfApi, repo: str) -> str | None:
    """Best-effort LFS/OID lookup; fallback to path hash if unavailable."""
    try:
        meta = api.get_paths_metadata(repo_id=repo, paths=[path], repo_type="dataset")
        if meta and meta[0] and getattr(meta[0], "lfs", None):
            return meta[0].lfs.get("oid", "").replace("sha256:", "")
    except Exception:
        pass
    # Deterministic fallback
    return hashlib.sha256(path.encode()).hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under data/ or root")
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)

    prefix = args.date if args.date.endswith("/") else f"{args.date}/"
    try:
        entries = api.list_repo_tree(
            repo_id=args.repo,
            path=prefix,
            repo_type="dataset",
            recursive=False,
        )
    except Exception:
        # Fallback: try root-level listing if date folder not found
        entries = api.list_repo_tree(
            repo_id=args.repo,
            path="",
            repo_type="dataset",
            recursive=False,
        )
        entries = [e for e in entries if e.path.startswith(prefix)]

    files = []
    for e in entries:
        if e.type != "file":
            continue
        sha256 = sha256_of_path(e.path, api, args.repo)
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{e.path}"
        files.append(
            {
                "path": e.path,
                "size": e.size,
                "sha256": sha256,
                "cdn_url": cdn_url,
            }
        )

    payload = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) `bin/dataset-enrich.sh` (updated)

Add CDN-mode and file-list support. Keep existing behavior when no list provided.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Normalize public dataset shards into {prompt,response} pairs.
#
# Usage:
#   # Full streaming (HF API)
#   HF_TOKEN=... ./bin/dataset-enrich.sh --shard 0/16 --out shard-000.jsonl
#
#   # CDN-only with pre-computed file list (recommended)
#   ./bin/dataset-enrich.sh --shard 0/16 --file-list file-list.json --out shard-000.jsonl

set -euo pipefail
export SHELL=/bin/bash

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:-}"
OUT=""
SHARD_SPEC=""
FILE_LIST=""
CDN_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --shard) SHARD_SPEC="$2"; shift 2 ;;
    --file-list) FILE_LIST="$2"; shift 2 ;;
    --cdn-only) CDN_ONLY=true; shift ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

if [[ -z "$OUT" || -z "$SHARD_SPEC" ]]; then
  echo "Usage: $0 --shard N/M --out out.jsonl [--file-list list.json] [--cdn-only]"
  exit 1
fi

SHARD_INDEX="${SHARD_SPEC%%/*}"
SHARD_TOTAL="${SHARD_SPEC##*/}"
if ! [[ "$SHARD_INDEX" =~ ^[0-9]+$ && "$SHARD_TOTAL" =~ ^[0-9]+$ ]]; then
  echo "Invalid shard spec: $SHARD_SPEC"
  exit 1
fi

# Deterministic assignment by slug hash
assign_shard() {
  local slug="$1"
  local hash
  hash=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( hash % SHARD_TOTAL ))
}

# Dedup helper (central md5 store)
DEDUPS_DB="./dedup.db"
python3 -c "
import sqlite3, sys, os
db = sys.argv[1]
os.makedirs(os.path.dirname(db) if os.path.dirname(db) else '.', exist_ok=True)
conn = sqlite3.connect(db)
conn.execute('CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)')
conn.commit()
conn.close()
" "$DEDUPS_DB"

is_seen() {
  local md5="$1"
  python3 -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
cur = conn.execute('SELECT 1 FROM seen WHERE md5=?', (sys.argv[2],))
print('1' if cur.fetchone() else '0')
conn.close()
" "$DEDUPS_DB" "$md5"
}

mark_seen() {
  local md5="$1"
  python3 -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('INSERT OR IGNORE INTO seen (md5) VALUES (?)', (sys.argv[2],))
conn.commit()
conn.close()
" "$DEDUPS_DB" "$md5"
}

# Normalize a single file into lines of {prompt,response}
# Supports multiple input schemas; projects to {prompt,response} only.
normalize_file() {
  local src="$1"
  local tmp
  tmp=$(mktemp)

  # Try parquet first (common), fallback to jsonl/csv
  if python
