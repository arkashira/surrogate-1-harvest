# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_files` with a deterministic pre-flight snapshot + CDN-only fetches. This eliminates HF API 429s, pyarrow CastError on mixed schemas, and removes per-shard recursive listing.

### Steps (1h 30m total)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — run once per date folder from Mac (or cron before the 16-shard matrix starts). Uses single `list_repo_tree(recursive=False)` per subfolder, emits `snapshot-{date}.json` containing `{path, size, etag, url}` for every file under `batches/public-merged/{date}/` or target folder. (20m)  
2. **Update `bin/dataset-enrich.sh`** — accept optional snapshot file; if provided, iterate URLs from snapshot and use `curl` (CDN) to stream each file; fallback to HF API only if snapshot missing. Remove `datasets.load_dataset` calls entirely. (30m)  
3. **Add lightweight CDN streamer** (`bin/stream_cdn.py`) — reads snapshot, yields lines from each remote file via full download (range requests not needed for parquet) and projects to `{prompt, response}` on the fly; never builds full parquet in memory. (20m)  
4. **Update GitHub Actions matrix** — add step before matrix to fetch snapshot artifact; pass `SNAPSHOT_PATH` to each shard job; keep `strategy: matrix: shard: [0..15]`. (10m)  
5. **Small fixes** — ensure `bin/dataset-enrich.sh` has `#!/usr/bin/env bash`, `set -euo pipefail`, and crontab uses `SHELL=/bin/bash`. (10m)

---

## Code Snippets

### 1) Snapshot generator (`bin/make-snapshot.py`)

```python
#!/usr/bin/env python3
"""
Create deterministic snapshot of public dataset files for a date folder.
Usage:
  HF_TOKEN=<token> python bin/make-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out snapshot-2026-05-02.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-05-02)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--prefix", help="Optional custom prefix (overrides date)")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)
    prefix = args.prefix or f"batches/public-merged/{args.date}/"
    # Non-recursive per folder to avoid heavy pagination
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=prefix,
        recursive=False,
        repo_type="dataset",
    )

    files = []
    for e in entries:
        if not e.path.endswith(".parquet"):
            continue
        files.append({
            "repo": args.repo,
            "path": e.path,
            "sha": getattr(e, "oid", None),
            "size": getattr(e, "size", None),
            "url": f"https://huggingface.co/datasets/{args.repo}/resolve/main/{e.path}",
        })

    snapshot = {
        "date": args.date,
        "prefix": prefix,
        "files": files,
    }

    with open(args.out, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Snapshot written to {args.out} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

### 2) Lightweight CDN streamer (`bin/stream_cdn.py`)

```python
#!/usr/bin/env python3
"""
Stream parquet files from snapshot via CDN and emit {prompt, response, slug} lines.
Usage:
  python bin/stream_cdn.py snapshot-2026-05-02.json | ...
"""
import json
import sys
import tempfile
import urllib.request
import pyarrow.parquet as pq
import hashlib

def emit_rows(path_or_url: str):
    # Download to temp file (keeps memory low; avoids mixed-schema issues via column projection)
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        urllib.request.urlretrieve(path_or_url, tmp_path)
        try:
            tbl = pq.read_table(tmp_path, columns=["prompt", "response"])
        except Exception:
            # Fallback: read full table if projection fails
            tbl = pq.read_table(tmp_path)
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            prompt = str(row.get("prompt", ""))
            response = str(row.get("response", ""))
            if not prompt or not response:
                continue
            slug = hashlib.md5(f"{prompt}{response}".encode()).hexdigest()
            print(json.dumps({"prompt": prompt, "response": response, "slug": slug}))
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: stream_cdn.py snapshot.json", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        snap = json.load(f)

    for fobj in snap.get("files", []):
        emit_rows(fobj["url"])

if __name__ == "__main__":
    main()
```

### 3) Updated `bin/dataset-enrich.sh` (key section)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD="${SHARD_ID:-0}"
SNAPSHOT_PATH="${SNAPSHOT_PATH:-}"

OUT_DIR="batches/public-merged/${DATE}"
TS="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD}-${TS}.jsonl"

mkdir -p "$OUT_DIR"

if [[ -n "$SNAPSHOT_PATH" && -f "$SNAPSHOT_PATH" ]]; then
  echo "Using snapshot $SNAPSHOT_PATH (CDN-only mode)"
  # Stream via CDN, shard filter, dedup
  python3 bin/stream_cdn.py "$SNAPSHOT_PATH" | \
  python3 -c "
import sys, json, hashlib
for line in sys.stdin:
    row = json.loads(line)
    # Deterministic shard by slug hash
    shard = int(hashlib.sha256(row['slug'].encode()).hexdigest(), 16) % 16
    if shard == ${SHARD}:
        print(json.dumps(row))
" | python3 -c "
import sys, json
from lib.dedup import DedupStore
dedup = DedupStore()
for line in sys.stdin:
    row = json.loads(line)
    if dedup.is_duplicate(row['slug']):
        continue
    dedup.mark(row['slug'])
    print(json.dumps({'prompt': row['prompt'], 'response': row['response'], 'slug': row['slug']}))
" > "$OUT_FILE"
else
  echo "WARNING: No snapshot provided; falling back to datasets.load_dataset (may hit API limits)"
  python3 -c "
import sys
from datasets import load_dataset
import hashlib, json
ds = load_dataset('${REPO}', name='default', split='train', streaming=True)
for row in ds:
    prompt = str(row.get('prompt', ''))
    response = str(row.get('response', ''))
    if not prompt or not response:
        continue
    slug = hashlib.md5((prompt + response).encode()).hexdigest()
    print(json.dumps({'prompt': prompt, 'response': response, 'slug': slug}))
" | python3 -c "

