# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes
1. Add `bin/list-date-files.py` — single Mac-side script that calls `list_repo_tree` once per date folder, saves `date-files.json`, and embeds it in workers so training uses CDN-only fetches with zero API calls during data load.
2. Update `bin/dataset-enrich.sh` to accept a file-list JSON (or fallback to current behavior) and use CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for downloads.
3. Add lightweight retry/backoff for CDN downloads and respect HF commit-cap sharding for uploads (hash slug → sibling repo).
4. Add status check + reuse for Lightning Studio when surrogate-1 training is invoked (if applicable).

### Why this is highest value
- Eliminates HF API 429s during ingestion/training (the biggest reliability risk).
- Makes shard workers independent of API pagination/rate limits after the initial listing.
- Fits in <2h: one small Python script + shell tweaks + tests.

---

## Concrete Implementation

### 1) `bin/list-date-files.py`
```python
#!/usr/bin/env python3
"""
Usage:
  python bin/list-date-files.py --repo axentx/surrogate-1-training-pairs \
    --date-folder 2026-05-02 --out date-files.json

Produces:
{
  "repo": "...",
  "date_folder": "2026-05-02",
  "files": [
    {"path": "2026-05-02/file1.parquet", "size": 12345},
    ...
  ],
  "generated_at": "2026-05-02T22:57:00Z"
}
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

CDN_BASE = "https://huggingface.co/datasets"

def list_date_files(repo_id: str, date_folder: str, recursive: bool = False):
    api = HfApi()
    entries = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=recursive)
    files = []
    for e in entries:
        if e.type == "file":
            files.append({
                "path": e.path,
                "size": e.size,
                "cdn_url": f"{CDN_BASE}/{repo_id}/resolve/main/{e.path}"
            })
    return files

def main():
    parser = argparse.ArgumentParser(description="List files in a date folder for CDN-only ingestion.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-1-training-pairs)")
    parser.add_argument("--date-folder", required=True, help="Date folder path in repo (e.g. 2026-05-02)")
    parser.add_argument("--out", default="date-files.json", help="Output JSON path")
    parser.add_argument("--recursive", action="store_true", help="List recursively (default: False)")
    args = parser.parse_args()

    try:
        files = list_date_files(args.repo, args.date_folder, recursive=args.recursive)
        payload = {
            "repo": args.repo,
            "date_folder": args.date_folder,
            "recursive": args.recursive,
            "files": files,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Wrote {len(files)} files to {args.out}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
```

Make executable:
```bash
chmod +x bin/list-date-files.py
```

---

### 2) Update `bin/dataset-enrich.sh` to support CDN-first ingestion
Key additions:
- Accept `--file-list date-files.json` to drive CDN downloads.
- Fallback to current behavior if no file list.
- Use `curl`/`wget` against CDN URLs to bypass HF API auth/rate limits.
- Lightweight retry and per-shard deterministic output naming.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated to support CDN-first ingestion via pre-generated file list.

set -euo pipefail

# Ensure consistent shell environment
export SHELL=/bin/bash

REPO=${REPO:-"axentx/surrogate-1-training-pairs"}
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
DATE_FOLDER=${DATE_FOLDER:-$(date +%Y-%m-%d)}
OUT_DIR=${OUT_DIR:-"output"}
FILE_LIST=${FILE_LIST:-""}  # optional: path to date-files.json

mkdir -p "${OUT_DIR}"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard-${SHARD_ID}] $*"
}

# Deterministic shard assignment by slug hash
shard_for_slug() {
  local slug=$1
  # simple deterministic hash -> bucket
  local hash=$(echo -n "$slug" | md5sum | awk '{print "0x" substr($1,1,8)}')
  echo $(( hash % TOTAL_SHARDS ))
}

download_cdn() {
  local url=$1
  local out=$2
  # Use curl with retry; CDN does not require auth
  curl -fsSL --retry 3 --retry-delay 2 --max-time 300 -o "${out}" "${url}" || {
    log "WARN: CDN download failed: ${url}"
    return 1
  }
}

process_file() {
  local rel_path=$1
  local cdn_url=$2

  local slug=$(basename "${rel_path}" .parquet)
  local my_bucket=$(shard_for_slug "${slug}")
  if [[ "${my_bucket}" != "${SHARD_ID}" ]]; then
    return 0
  fi

  local tmp=$(mktemp)
  if ! download_cdn "${cdn_url}" "${tmp}"; then
    rm -f "${tmp}"
    return 1
  fi

  # Placeholder: project to {prompt,response} and normalize per schema
  # Keep minimal projection here; heavy schema handling can be in Python helper
  # For now, produce one JSONL entry per row with attribution in filename only
  local ts=$(date -u +%Y%m%d%H%M%S)
  local out_name="shard${SHARD_ID}-${ts}.jsonl"
  local out_path="${OUT_DIR}/${out_name}"

  # Example projection using python (fast and schema-safe)
  python3 -c "
import sys, json, pyarrow.parquet as pq
try:
    tbl = pq.read_table('${tmp}')
    cols = tbl.column_names
    # Heuristic: find prompt/response-like columns
    prompt_col = next((c for c in cols if 'prompt' in c.lower()), None)
    response_col = next((c for c in cols if 'response' in c.lower()), None)
    if prompt_col and response_col:
        for batch in tbl.to_batches():
            pc = batch.column(cols.index(prompt_col)).to_pylist()
            rc = batch.column(cols.index(response_col)).to_pylist()
            for p, r in zip(pc, rc):
                if p is not None and r is not None:
                    print(json.dumps({'prompt': p, 'response': r}))
    else:
        # fallback: dump first two text columns
        text_cols = [c for c in cols if 'text' in c.lower()]
        if len(text_cols) >= 2:
            for batch in tbl.to_batches():
                c0 = batch.column(cols.index(text_cols[0])).to_pylist()
                c1 = batch.column(cols.index(text_cols[1])).to_pylist()
                for a,b in zip(c0,c1):
                    if a is not None and b is not None:
                        print(json.dumps({'prompt': a, 'response': b}))
except Exception as e:
    sys.stderr.write(f'Projection error: {e}\\n')
  " >> "${out
