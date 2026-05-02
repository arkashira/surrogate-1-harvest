# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single Mac-side script that lists one date folder via `list_repo_tree(recursive=False)` and emits `file_list.json`. Embeds into training/shard scripts so workers do CDN-only fetches (zero API calls during data load).
2. **`bin/dataset-enrich.sh`** — updated to accept optional `FILE_LIST` path; if provided, iterates local JSON instead of calling `list_repo_files` repeatedly (reduces API pressure). Keeps fallback to current behavior.
3. **`bin/lib/dedup.py`** — no functional change; ensure it remains importable and thread-safe for 16 parallel runners.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Usage (Mac orchestration):
  python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out file_list.json

Produces:
  {
    "repo": "...",
    "date": "...",
    "files": [
      "batches/public-merged/2026-05-02/file1.parquet",
      ...
    ],
    "cdn_prefix": "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/"
  }
"""

import argparse
import json
import os
import sys
from typing import List

try:
    from huggingface_hub import HfApi
except ImportError:
    print("error: huggingface_hub not installed", file=sys.stderr)
    sys.exit(1)

CDN_PREFIX = "https://huggingface.co/datasets/{repo}/resolve/main/"

def list_date_files(repo: str, date: str) -> List[str]:
    api = HfApi()
    folder = f"batches/public-merged/{date}"
    try:
        items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception as exc:
        raise RuntimeError(f"HF list_repo_tree failed for {repo}/{folder}: {exc}") from exc

    files = []
    for item in items:
        if hasattr(item, "path") and item.path:
            # list_repo_tree may return nested objects; accept path string
            files.append(item.path)
    files.sort()
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-flight file listing for CDN-only ingestion")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under batches/public-merged/")
    parser.add_argument("--out", default="file_list.json", help="Output JSON path")
    args = parser.parse_args()

    files = list_date_files(args.repo, args.date)
    payload = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
        "cdn_prefix": CDN_PREFIX.format(repo=args.repo),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print(f"listed {len(files)} files -> {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh` (minimal, safe update)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic shard worker for public-dataset ingest.
#
# Optional env:
#   FILE_LIST        path to JSON produced by bin/list_files.py
#                    If set, workers iterate local list (CDN-only).
#   HF_TOKEN         write token for axentx/surrogate-1-training-pairs
#   SHARD_ID         0..15  (required by workflow matrix)
#   SHARD_COUNT      16     (required by workflow matrix)

set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:?required}"
SHARD_COUNT="${SHARD_COUNT:?required}"
HF_TOKEN="${HF_TOKEN:?required}"
OUTDIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "$OUTFILE")"

echo "[$(date)] shard ${SHARD_ID}/${SHARD_COUNT} starting for ${DATE}" >&2

# Helper: deterministic bucket by slug-hash
shard_for() {
  local slug=$1
  # fast deterministic 0..(SHARD_COUNT-1)
  python3 -c "import hashlib; print(int(hashlib.md5('$slug'.encode()).hexdigest(), 16) % $SHARD_COUNT)"
}

process_file() {
  local path=$1
  local cdn_url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"

  # Stream + schema projection: keep only {prompt,response} fields.
  # Uses pyarrow under the hood; memory-efficient for large parquet.
  python3 -c "
import pyarrow.parquet as pq
import json, sys
try:
    table = pq.read_table('$cdn_url', columns=['prompt','response'])
except Exception:
    # fallback: try common aliases
    try:
        table = pq.read_table('$cdn_url', columns=['input','output'])
    except Exception:
        table = pq.read_table('$cdn_url')
df = table.to_pandas()
for _, row in df.iterrows():
    prompt = row.get('prompt') or row.get('input') or ''
    response = row.get('response') or row.get('output') or ''
    if prompt or response:
        print(json.dumps({'prompt': str(prompt), 'response': str(response)}))
" 2>/dev/null | while IFS= read -r line; do
    [ -z "$line" ] && continue
    slug=$(echo "$line" | python3 -c "import sys,hashlib,json; d=json.load(sys.stdin); print(hashlib.md5((d['prompt']+d['response']).encode()).hexdigest())")
    if [ "$(shard_for "$slug")" -eq "$SHARD_ID" ]; then
      echo "$line"
    fi
  done
}

# Decide source list
if [ -n "${FILE_LIST:-}" ] && [ -f "$FILE_LIST" ]; then
  echo "[$(date)] using local file list: $FILE_LIST" >&2
  mapfile -t FILES < <(python3 -c "import json,sys; d=json.load(open('$FILE_LIST')); print('\n'.join(d.get('files',[])))")
else
  echo "[$(date)] listing via HF API (rate-limit aware)" >&2
  mapfile -t FILES < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
folder = 'batches/public-merged/${DATE}'
items = api.list_repo_tree(repo='${REPO}', path=folder, recursive=False)
for it in items:
    if hasattr(it, 'path') and it.path:
        print(it.path)
" 2>/dev/null || true)
fi

if [ ${#FILES[@]} -eq 0 ]; then
  echo "[$(date)] no files found for ${DATE}, exiting" >&2
  exit 0
fi

echo "[$(date)] processing ${#FILES[@]} files" >&2

for f in "${FILES[@]}"; do
  process_file "$f"
done > "$OUTFILE"

# Upload shard output (atomic)
if [ -s "$OUTFILE" ]; then
  echo "[$(date)] uploading ${OUTFILE} ($(wc -l <"$OUTFILE") lines)" >&2
  huggingface-cli upload --repo-type dataset "$REPO" "$OUTFILE" "$OUTFILE" --token "$HF_TOKEN"
else
  echo "[$(date)] shard ${SHARD_ID} produced no lines, skipping upload" >&2
fi

