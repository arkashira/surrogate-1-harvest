# surrogate-1 / quality

## Final Consolidated Implementation (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

---

### Changes (integrated, no contradictions)

1. **Add `bin/list-date-files.py`** — single Mac-side script that calls `list_repo_tree` once per date folder, saves `file-list-<date>.json` to repo. Embed this list in training and worker scripts so Lightning training and GitHub runners perform **CDN-only fetches** with zero API calls during data load.

2. **Update `bin/dataset-enrich.sh`** to accept an optional file-list JSON; if provided, workers iterate the list and filter by `shard_id = hash(slug) % 16` instead of calling `list_repo_files`/`list_repo_tree` (avoids 429s and pagination costs).

3. **Add `lib/cdn_stream.py`** helper that downloads via `https://huggingface.co/datasets/.../resolve/main/...` with streaming + retries (no auth header) and projects to `{prompt, response}` on the fly.

4. **Update training launcher** (`lightning_train.py` or equivalent) to load the embedded file list and use CDN URLs for `IterableDataset` — removes HF API auth/rate pressure during training epochs.

5. **Update `ingest.yml` workflow** to run the `list-date-files.py` script before launching the shard workers. This ensures the file list is updated before each ingestion run.

6. **Add README note and a cron-friendly one-liner** to refresh file lists after new dates appear.

---

### Code Snippets

#### 1) `bin/list-date-files.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in surrogate-1-training-pairs.
Run from Mac (or any dev machine) after rate-limit window clears.

Usage:
  python bin/list-date-files.py --repo axentx/surrogate-1-training-pairs --date 2026-05-02 --out file-list-2026-05-02.json
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_file_list(repo: str, date: str, out_path: str):
    api = HfApi()
    prefix = f"{date}/"
    try:
        tree = api.list_repo_tree(repo=repo, path=prefix, recursive=False)
    except Exception as exc:
        print(f"Error listing {repo}@{prefix}: {exc}", file=sys.stderr)
        sys.exit(1)

    entries = []
    for item in tree:
        if item.rfilename.endswith((".parquet", ".jsonl", ".json")):
            entries.append({
                "path": item.rfilename,
                "cdn_url": CDN_TEMPLATE.format(repo=repo, path=item.rfilename),
                "size": getattr(item, "size", None),
            })

    payload = {
        "repo": repo,
        "date": date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": sorted(entries, key=lambda x: x["path"]),
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(entries)} entries to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List date folder files for CDN-only ingestion.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()
    build_file_list(repo=args.repo, date=args.date, out_path=args.out)
```

#### 2) `lib/cdn_stream.py`
```python
import io
import json
import warnings
from typing import Iterator, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from datasets import Features

CDN_RETRY = 3
CDN_TIMEOUT = 30

def cdn_get(url: str, stream: bool = True) -> requests.Response:
    for attempt in range(1, CDN_RETRY + 1):
        try:
            resp = requests.get(url, stream=stream, timeout=CDN_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            if attempt == CDN_RETRY:
                raise
            warnings.warn(f"CDN fetch failed ({exc}), retry {attempt}/{CDN_RETRY}")
    raise RuntimeError("unreachable")

def stream_parquet_rows(cdn_url: str, columns=("prompt", "response")) -> Iterator[Dict[str, Any]]:
    """Stream rows from a remote parquet file via CDN with projection."""
    resp = cdn_get(cdn_url, stream=True)
    with io.BytesIO() as bio:
        for chunk in resp.iter_content(chunk_size=8192):
            bio.write(chunk)
        bio.seek(0)
        table = pq.read_table(bio, columns=columns)
        for batch in table.to_batches(max_chunksize=1024):
            cols = {name: batch.column(name).to_pylist() for name in columns}
            for i in range(batch.num_rows):
                yield {k: cols[k][i] for k in columns}

def stream_jsonl_rows(cdn_url: str) -> Iterator[Dict[str, Any]]:
    resp = cdn_get(cdn_url, stream=True)
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        yield json.loads(line)

def project_pair(row: Dict[str, Any]) -> Dict[str, str]:
    """Normalize heterogeneous schemas to {prompt, response}."""
    prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
    response = row.get("response") or row.get("output") or row.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def iter_cdn_shard(file_list, shard_id: int, total_shards: int = 16):
    """Iterate CDN items for a deterministic shard assignment."""
    for entry in file_list["files"]:
        slug = entry["path"]
        if hash(slug) % total_shards != shard_id:
            continue
        cdn_url = entry["cdn_url"]
        if cdn_url.endswith(".parquet"):
            yield from stream_parquet_rows(cdn_url)
        elif cdn_url.endswith(".jsonl"):
            yield from stream_jsonl_rows(cdn_url)
        else:
            continue
```

#### 3) `bin/dataset-enrich.sh` (updated)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Optional: pass a pre-fetched file list JSON to avoid HF API during workers.
# If not provided, falls back to HF API (may hit 429s).
FILE_LIST="${FILE_LIST:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  echo "Using CDN-only ingestion from $FILE_LIST (shard $SHARD_ID/$TOTAL_SHARDS)"
  python -c "
import json, sys
from lib.cdn_stream import iter_cdn_shard
with open(sys.argv[1]) as f:
    file_list = json.load(f)
for item in iter_cdn_shard(file_list, shard_id=int(sys.argv[2]), total_shards=int(sys.argv[3])):
    print(json.dumps(item))
" "$FILE_LIST" "$SHARD_ID" "$TOTAL_SHARDS"
else
  echo "No FILE_LIST provided; falling back to HF API (may hit 429s)."
  # Existing HF-API-based listing logic here (kept for fallback).
fi
```

#### 4) Training launcher snippet (conceptual)
```python
from lib.cdn_stream import iter_cdn_shard
import json

with open("file-list-2026-05-02.json")
