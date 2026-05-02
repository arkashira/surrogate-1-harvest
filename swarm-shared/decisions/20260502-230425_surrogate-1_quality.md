# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + **CDN-only ingestion** (no fallback) to eliminate HF API 429s during training and make shard workers fully resilient and reproducible.

### Changes (merged & resolved)
1. **`bin/list-date-files.py`** — single script that calls `list_repo_tree` once per date folder, saves `date-files.json`, and is run once per workflow.
2. **`bin/dataset-enrich.sh`** — accept file-list JSON and fetch **exclusively via CDN URLs**; remove any `datasets.load_dataset` or HF API data calls. Deterministic shard assignment by `hash(slug) % 16`.
3. **`lib/cdn_download.py`** — small reusable helper with retry/back-off and timeouts (replaces inline curl for portability and testability).
4. **GitHub Actions** — add `prepare` job that produces `date-files.json` once; matrix `ingest` job downloads the artifact and passes `SHARD_ID`/`TOTAL_SHARDS`.
5. **Training-script guidance** (optional) — show how to consume the same file list for CDN-only `torch.utils.data.IterableDataset` or Lightning `DataModule`.

### Why this is highest value and correctness choice
- **Eliminates HF API 429s** (the biggest reliability risk) by removing all data-plane API calls during ingest/training.
- **CDN-only** (no fallback) prevents accidental HF API traffic; CDN has much higher rate limits and is purpose-built for bulk reads.
- **Deterministic snapshot** via shared file list ensures reproducibility and identical data across workers.
- **Fits <2h**: ~60–80 lines Python, ~40–60 lines Bash/workflow, minimal dependencies.

---

## Code Snippets

### 1) `bin/list-date-files.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic file list for a date folder.

Usage:
  HF_TOKEN=... \
  python bin/list-date-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date-folder 2026-05-02 \
    --out date-files.json
"""
import argparse
import json
import os
import time
from huggingface_hub import HfApi

HF_TOKEN = os.getenv("HF_TOKEN", "")

def list_date_files(repo_id: str, date_folder: str, out_path: str):
    api = HfApi(token=HF_TOKEN or None)
    # Single non-recursive call per date folder
    tree = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = [{"path": f.rfilename, "size": f.size} for f in tree if f.type == "file"]

    payload = {
        "repo": repo_id,
        "date_folder": date_folder,
        "files": files,
        "listed_at_utc": int(time.time()),
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--date-folder", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    list_date_files(args.repo, args.date_folder, args.out)
```

### 2) `lib/cdn_download.py`
```python
import time
import urllib.request
import urllib.error
import hashlib
import os
from typing import Optional

def stable_hash_int(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)

def cdn_fetch(url: str, out_path: str, max_retries: int = 5, timeout: int = 30) -> bool:
    attempt = 0
    while attempt < max_retries:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "training-ingest/1.0 (+https://huggingface.co/datasets)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                with open(out_path, "wb") as f:
                    f.write(resp.read())
            return True
        except urllib.error.HTTPError as e:
            # Do not retry 4xx except 429; 404 is permanent
            if e.code in (400, 401, 403, 404, 422):
                return False
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

        attempt += 1
        sleep_sec = min(2 ** attempt, 60)
        time.sleep(sleep_sec)
    return False
```

### 3) `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# CDN-only ingestion with deterministic sharding.
# Usage:
#   FILE_LIST=date-files.json \
#   SHARD_ID=0 \
#   TOTAL_SHARDS=16 \
#   OUT=shard-0.jsonl \
#   bash bin/dataset-enrich.sh

set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
FILE_LIST="${FILE_LIST:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT="${OUT:-/tmp/shard.jsonl}"

if [[ -z "$FILE_LIST" || ! -f "$FILE_LIST" ]]; then
  echo "ERROR: FILE_LIST must point to JSON produced by list-date-files.py" >&2
  exit 1
fi

# Deterministic shard assignment by filename slug
shard_for() {
  local slug="$1"
  python3 -c "import hashlib; print(int(hashlib.sha256(b'$slug').hexdigest(), 16) % $TOTAL_SHARDS)"
}

# Minimal projection: {prompt,response} only
project_parquet_to_jsonl() {
  local parquet_path="$1"
  python3 -c "
import sys, json
try:
    import pyarrow.parquet as pq
except ImportError:
    print('ERROR: pyarrow required for projection', file=sys.stderr)
    sys.exit(1)

tbl = pq.read_table('$parquet_path', columns=['prompt','response'])
for i in range(tbl.num_rows):
    rec = {k: tbl[k][i].as_py() for k in ('prompt','response')}
    rec = {k: (v if v is not None else '') for k, v in rec.items()}
    print(json.dumps(rec, ensure_ascii=False))
"
}

# Main
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

jq -r '.files[].path' "$FILE_LIST" | while read -r rel_path; do
  slug=$(basename "$rel_path" | sed 's/\.[^.]*$//')
  if [[ $(shard_for "$slug") -ne $SHARD_ID ]]; then
    continue
  fi

  url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  local_file="${TMP_DIR}/$(basename "$rel_path")"

  if python3 -c "
import sys
sys.path.insert(0, 'lib')
from cdn_download import cdn_fetch
import sys
ok = cdn_fetch('$url', '$local_file')
sys.exit(0 if ok else 1)
"; then
    project_parquet_to_jsonl "$local_file"
  else
    echo "WARN: CDN fetch failed for $rel_path, skipping" >&2
  fi
done > "$OUT"

echo "Shard $SHARD_ID wrote $(wc -l < "$OUT") records to $OUT"
```

### 4) `.github/workflows/ingest.yml` (excerpt)
```yaml
name: Ingest

on:
  workflow_dispatch:
  schedule:
    - cron: '0 6 * * *'  # daily UTC

jobs:
  prepare:
    runs-on: ubuntu-latest
    outputs:
      date-folder: ${{ steps.date.outputs.folder }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python
