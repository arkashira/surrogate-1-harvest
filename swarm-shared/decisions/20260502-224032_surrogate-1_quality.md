# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Core Changes (3 files, ~150 lines total)

1. **`bin/list_files.py`** — Mac-side script that calls `list_repo_tree` once per date folder, saves deterministic `file-list-<date>.json` to repo (single source of truth). **This is the only place we use HF API.**

2. **`lib/cdn_stream.py`** — Streaming parquet reader over CDN URLs (no auth, no API quota) with projection to `{prompt, response}` and optional md5 dedup.

3. **`bin/dataset-enrich.sh`** — Updated worker script that:
   - Accepts `FILE_LIST` (committed manifest) and `SHARD_ID`
   - Deterministic shard assignment: `hash(path) % 16 == SHARD_ID`
   - CDN-only downloads with `curl --retry 3 --retry-delay 5 --max-time 300`
   - Stream-parse → dedup → append to `shard-<N>.jsonl`
   - Single commit/upload per shard

### Why this is highest value
- **Eliminates HF API 429s** during ingestion (workers never call `/api/`).
- **Preserves 16-shard parallelism** in surrogate-1-runner.
- **Fits existing layout**; no infra changes.
- **Immediate payoff** for cron runs and manual triggers.

---

## Code

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Usage (Mac, after rate-limit window clears):
  python bin/list_files.py --repo axentx/surrogate-1-training-pairs --date 2026-05-02 --out file-list-2026-05-02.json

Produces:
  {
    "date": "2026-05-02",
    "files": [
      "public-merged/2026-05-02/file1.parquet",
      "batches/mirror-merged/2026-05-02/file2.parquet",
      ...
    ]
  }
"""
import argparse
import json
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    prefixes = [
        f"public-merged/{args.date}",
        f"batches/mirror-merged/{args.date}",
    ]

    files = []
    for prefix in prefixes:
        try:
            items = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False)
            for item in items:
                if item.rfilename.endswith((".jsonl", ".parquet")):
                    files.append(f"{prefix}/{item.rfilename}")
        except Exception as e:
            print(f"WARN: {prefix} -> {e}", file=sys.stderr)

    manifest = {"date": args.date, "files": sorted(files)}
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

### 2) `lib/cdn_stream.py`

```python
import io
import json
import hashlib
import pyarrow.parquet as pq
import requests

from lib.dedup import is_duplicate, store_hash  # existing dedup module

CDN_ROOT = "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"

def stream_cdn_parquet(cdn_url: str, columns=("prompt", "response")):
    """
    Stream a parquet file from CDN and yield rows as dicts.
    Retries and timeouts handled by caller.
    """
    with requests.get(cdn_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_content(chunk_size=8192):
            buf.write(chunk)
        buf.seek(0)
        table = pq.read_table(buf, columns=columns)
        for batch in table.to_batches(max_chunksize=1024):
            for row in batch.to_pylist():
                yield row

def process_and_dedup_row(row):
    """Return JSON-serializable record or None if duplicate/invalid."""
    prompt = (row.get("prompt") or "").strip()
    response = (row.get("response") or "").strip()
    if not prompt or not response:
        return None
    blob = f"{prompt}\n{response}".encode()
    md5 = hashlib.md5(blob).hexdigest()
    if is_duplicate(md5):
        return None
    store_hash(md5)
    return {"prompt": prompt, "response": response}
```

### 3) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash

HF_REPO="datasets/axentx/surrogate-1-training-pairs"
DATE=${DATE:-$(date +%F)}
FILE_LIST=${FILE_LIST:-file-list-${DATE}.json}
SHARD_ID=${SHARD_ID:-0}
OUTDIR="output/shard-${SHARD_ID}"
mkdir -p "$OUTDIR"

# Deterministic shard assignment from manifest
mapfile -t ASSIGNED < <(
  python3 -c "
import json, hashlib, sys
manifest = json.load(open('$FILE_LIST'))
for item in manifest['files']:
    path = item if isinstance(item, str) else item['path']
    if hashlib.sha256(path.encode()).hexdigest().isdigit():
        h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    else:
        h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    if h % 16 == ${SHARD_ID}:
        print(path)
"
)

echo "Assigned ${#ASSIGNED[@]} files to shard ${SHARD_ID}"

OUTFILE="${OUTDIR}/shard-${SHARD_ID}-$(date +%H%M%S).jsonl"
> "$OUTFILE"

for relpath in "${ASSIGNED[@]}"; do
    cdn_url="${CDN_ROOT}/${relpath}"
    echo "Processing: ${relpath}"

    python3 -c "
import sys, json
from lib.cdn_stream import stream_cdn_parquet, process_and_dedup_row

url = sys.argv[1]
try:
    for row in stream_cdn_parquet(url):
        rec = process_and_dedup_row(row)
        if rec:
            print(json.dumps(rec))
except Exception as e:
    print(f'ERROR: {url} -> {e}', file=sys.stderr)
    sys.exit(0)  # non-fatal per file
" "$cdn_url" >> "$OUTFILE" || true
done

# Single commit/upload per shard
if [[ -s "$OUTFILE" ]]; then
    git config user.name "github-actions"
    git config user.email "actions@github.com"
    git add "$OUTFILE"
    git commit -m "shard-${SHARD_ID}: ${DATE} enrichment" || true
    # Prefer HF dataset repo upload; fallback to release
    python3 -c "
from huggingface_hub import upload_file
try:
    upload_file(
        path_or_fileobj='$OUTFILE',
        path_in_repo='${OUTFILE}',
        repo_id='${HF_REPO}',
        repo_type='dataset'
    )
except Exception as e:
    print(f'HF upload failed: {e}')
" || true
fi

echo "Shard ${SHARD_ID} complete: ${OUTFILE}"
```

---

## Rollout (≤2h)

1. **Commit new files**  
   ```bash
   git add bin/list_files.py lib/cdn_stream.py bin/dataset-enrich.sh
   git commit -m "CDN-only ingestion + deterministic shard manifest"
   ```

2. **Generate and commit manifest** (run once on Mac after rate-limit window)  
   ```
