# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~110 lines total)

1. **`bin/list_files.py`** — single API call (post-rate-limit window) that lists one date folder via `list_repo_tree(recursive=False)` and writes `file_list.json`. Embed this in the workflow so workers use CDN URLs only.
2. **`bin/dataset-enrich.sh`** — accept an optional `FILE_LIST` path; if provided, read JSON and stream from CDN (`/resolve/main/...`) instead of `load_dataset(streaming=True)`. Keep existing HF write path for outputs.
3. **`.github/workflows/ingest.yml`** — add a one-time "list" job (or step) that produces `file_list.json` as an artifact, then pass it to the 16 shard matrix jobs.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Produce file_list.json for a single date folder.
Run from Mac (or once per cron tick) after rate-limit window clears.

Usage:
  python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --path batches/public-merged/2026-05-02 \
    --out file_list.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # recursive=False avoids 100x pagination on big repos
    tree = api.list_repo_tree(repo_id=args.repo, path=args.path, recursive=False)
    files = [
        {
            "path": node.path,
            "cdn_url": f"https://huggingface.co/datasets/{args.repo}/resolve/main/{node.path}"
        }
        for node in tree
        if not node.path.endswith("/")
    ]

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "repo": args.repo,
        "path": args.path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(files),
        "files": files
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh` (partial patch)

Add CDN fallback and deterministic shard selection. Keep existing HF write logic unchanged.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic shard worker: consume FILE_LIST (JSON) and stream via CDN.

set -euo pipefail
export SHELL=/bin/bash

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"   # optional: path to file_list.json
WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

# Deterministic shard assignment by filename hash
shard_of() {
  local slug="$1"
  # quick deterministic hash; 0..N_SHARDS-1
  python3 -c "print(hash('$slug') % $N_SHARDS)" 2>/dev/null | tr -d -
}

process_file() {
  local cdn_url="$1"
  local rel_path="$2"
  local slug
  slug="$(basename "$rel_path" .parquet)"

  [[ "$(shard_of "$slug")" != "$SHARD_ID" ]] && return 0

  # Download via CDN (no Authorization header -> bypass /api/ rate limit)
  local src
  src="$(mktemp "$WORK_DIR/src.XXXXXX")"
  curl -fsSL --retry 3 "$cdn_url" -o "$src"

  # Project to {prompt,response} only; drop extra schema columns
  python3 - "$src" "$slug" <<'PY'
import pyarrow.parquet as pq
import sys, json, hashlib

src, slug = sys.argv[1], sys.argv[2]
try:
    tbl = pq.read_table(src, columns=["prompt", "response"])
except (ValueError, KeyError, OSError):
    # Heterogeneous schema: try common aliases
    try:
        tbl = pq.read_table(src)
        if "text" in tbl.column_names and "completion" in tbl.column_names:
            tbl = tbl.select(["text", "completion"]).rename_columns(["prompt", "response"])
        elif "input" in tbl.column_names and "output" in tbl.column_names:
            tbl = tbl.select(["input", "output"]).rename_columns(["prompt", "response"])
        else:
            cols = [c for c in tbl.column_names if "prompt" in c.lower() or "text" in c.lower()]
            resp = [c for c in tbl.column_names if "response" in c.lower() or "completion" in c.lower()]
            if cols and resp:
                tbl = tbl.select([cols[0], resp[0]]).rename_columns(["prompt", "response"])
            else:
                raise
    except Exception:
        sys.exit(0)

df = tbl.to_pandas()
for _, row in df.iterrows():
    prompt = str(row.get("prompt") or "")
    response = str(row.get("response") or "")
    if not prompt.strip() or not response.strip():
        continue
    payload = json.dumps({"prompt": prompt, "response": response}, ensure_ascii=False)
    md5 = hashlib.md5(payload.encode()).hexdigest()
    print(json.dumps({"md5": md5, "slug": slug, "payload": payload}, ensure_ascii=False))
PY
}

export -f shard_of process_file

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  # CDN-only mode: no HF API calls during ingestion
  echo "Using CDN list from $FILE_LIST"
  jq -r '.files[] | "\(.cdn_url) \(.path)"' "$FILE_LIST" | \
    xargs -P "$(nproc)" -I{} bash -c 'process_file $1 $2' _ {}
else
  # Fallback: original streaming behavior (may hit 429)
  echo "WARNING: FILE_LIST not provided — falling back to HF streaming (risk 429)"
  python3 -c "
from datasets import load_dataset
import sys, hashlib, json
for row in load_dataset('$REPO', split='train', streaming=True):
    prompt = str(row.get('prompt') or row.get('text') or '')
    response = str(row.get('response') or row.get('completion') or '')
    if not prompt.strip() or not response.strip():
        continue
    payload = json.dumps({'prompt': prompt, 'response': response}, ensure_ascii=False)
    md5 = hashlib.md5(payload.encode()).hexdigest()
    print(json.dumps({'md5': md5, 'slug': 'streamed', 'payload': payload}, ensure_ascii=False))
"
fi | python3 -c "
import sys, json, os
from lib.dedup import DedupStore

dedup = DedupStore()
batch = []
for line in sys.stdin:
    rec = json.loads(line)
    if dedup.is_dup(rec['md5']):
        continue
    dedup.mark(rec['md5'])
    batch.append(json.dumps({'prompt': json.loads(rec['payload'])['prompt'],
                             'response': json.loads(rec['payload'])['response']}))
    if len(batch) >= 1000:
        print('\n'.join(batch))
        batch = []
if batch:
    print('\n'.join(batch))
" > "$WORK_DIR/out.jsonl"

# Upload to HF (existing behavior)
if [[ -s "$WORK_DIR/out.jsonl" ]]; then
  DATE_TAG="$(date +%Y-%m-%d)"
  TIME_TAG="$(date +%H%M
