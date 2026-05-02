# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-shot Mac/CI script that calls `list_repo_tree` per date folder, saves `file-list-<date>.json` with CDN URLs.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON; if present, workers stream from CDN URLs (no `load_dataset`/`list_repo_files` during ingestion).
3. Add deterministic sibling-repo sharding for writes (hash slug → 1 of 5 repos) to avoid HF commit cap (128/hr/repo).
4. Keep existing SQLite dedup store as source of truth; workers remain stateless per run.

### Files to change
- `bin/list-files.py` (new)
- `bin/dataset-enrich.sh` (modify)
- `lib/dedup.py` (no change)
- `requirements.txt` (add `requests` if not present)

---

### 1) `bin/list-files.py` (new)

```python
#!/usr/bin/env python3
"""
Generate CDN file-list JSON for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=... python bin/list-files.py --date 2026-05-02 --out file-list-2026-05-02.json

Output schema:
{
  "repo": "datasets/axentx/surrogate-1-training-pairs",
  "date": "2026-05-02",
  "files": [
    {
      "path": "batches/public-raw/2026-05-02/slug1234.parquet",
      "cdn_url": "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/batches/public-raw/2026-05-02/slug1234.parquet",
      "size": 123456,
      "sha256": "..."
    },
    ...
  ]
}
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi


def list_date_files(repo_owner: str, repo_name: str, date: str, out_path: str) -> None:
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    repo = f"{repo_owner}/{repo_name}"

    folders = ["raw", "curated", "mirror-merged"]
    files = []

    for folder in folders:
        path = f"{date}/{folder}"
        try:
            items = api.list_repo_tree(repo, path=path, recursive=False)
        except Exception as exc:
            print(f"Warning: {path} not found or inaccessible: {exc}", file=sys.stderr)
            continue

        for item in items:
            if item.type == "file":
                files.append(
                    {
                        "path": item.path,
                        "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}",
                        "size": getattr(item, "size", None),
                        "sha256": getattr(item, "sha256", None),
                    }
                )

    manifest = {
        "repo": f"datasets/{repo}",
        "date": date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} entries to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN file-list JSON for a date folder.")
    parser.add_argument("--owner", default="axentx", help="Repo owner")
    parser.add_argument("--repo", default="surrogate-1-training-pairs", help="Repo name")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    list_date_files(args.owner, args.repo, args.date, args.out)
```

Make executable:

```bash
chmod +x bin/list-files.py
```

Add to `requirements.txt` if not present:

```
requests
huggingface-hub>=0.22.0
```

---

### 2) Updated `bin/dataset-enrich.sh` (CDN-only ingestion + sibling-repo sharding)

Key changes:
- Accept `--file-list file-list-YYYY-MM-DD.json`.
- If file-list provided, workers stream via CDN URLs (no `load_dataset`/`list_repo_files` during ingestion).
- Deterministic sibling-repo sharding for writes: `slug-hash % 5` → pick sibling repo (`axentx/surrogate-1-training-pairs-shard0..4`) to stay under 128 commits/hr/repo.
- Keep existing SQLite dedup store as source of truth; workers remain stateless per run.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: CDN-only ingestion + sibling-repo sharding
set -euo pipefail

HF_TOKEN="${HF_TOKEN:-}"
REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"
WORK_DIR="${WORK_DIR:-/tmp/surrogate-ingest}"
SIBLING_COUNT="${SIBLING_COUNT:-5}"
SIBLING_REPO_PREFIX="${SIBLING_REPO_PREFIX:-axentx/surrogate-1-training-pairs-shard}"

mkdir -p "$WORK_DIR"

# Determine target repo by slug hash (deterministic sibling sharding)
pick_sibling() {
  local slug="$1"
  local hash
  # deterministic 0..(SIBLING_COUNT-1)
  hash=$(echo -n "$slug" | md5sum | tr -d ' -' | tr '[:lower:]' '[:upper:]')
  # take first 8 hex chars as int
  local num=$((16#${hash:0:8}))
  echo $((num % SIBLING_COUNT))
}

# Stream and normalize a single file via CDN (no HF API)
stream_via_cdn() {
  local path="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"
  # delegate to python for schema-aware projection to {prompt,response}
  python3 - <<PY
import sys, json, pyarrow.parquet as pq, tempfile, os, requests

path = sys.argv[1]
url = sys.argv[2]
work_dir = sys.argv[3]

# download to temp file (streaming)
out_path = os.path.join(work_dir, os.path.basename(path))
with requests.get(url, stream=True, timeout=60) as resp:
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192*1024):
            if chunk:
                f.write(chunk)

# project to {prompt,response} only
try:
    table = pq.read_table(out_path, columns=["prompt", "response"])
except Exception:
    # fallback: try common column names
    try:
        table = pq.read_table(out_path)
        cols = table.column_names
        prompt_col = next((c for c in cols if "prompt" in c.lower()), cols[0])
        response_col = next(
            (c for c in cols if "response" in c.lower() or "completion" in c.lower()),
            cols[-1],
        )
        table = table.select([prompt_col, response_col]).rename_columns(["prompt", "response"])
    except Exception as e:
        print(f"Cannot project {path}: {e}", file=sys.stderr)
        sys.exit(0)

for batch in table.to_batches():
    for row in batch.to_pylist():
        if row.get("prompt") and row.get("response"):
            print(json.dumps(row, ensure_ascii=False))
PY "$path" "$
