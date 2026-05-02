# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-time Mac/CI script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path + size + sha256).  
2. Add `lib/cdn_stream.py` — robust CDN fetcher with retries and schema-agnostic projection to `{prompt, response}`.  
3. Update `bin/dataset-enrich.sh` to accept optional `FILE_LIST`; if present, workers stream via CDN URLs instead of `load_dataset`.  
4. Add `requirements-cdn.txt` (requests + pyarrow) and update `requirements.txt` to pin versions.  
5. Update `.github/workflows/ingest.yml` to run `list-files.py` in a prior job, pass the JSON to the matrix via artifact, and set `FILE_LIST` for each shard.

Total time: ~90 min (45 min code, 45 min test/PR).

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/list-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-04-29 \
    --out file-list.json

Writes:
[
  {"path": "raw/abc/0000.parquet", "size": 12345, "sha256": "..."},
  ...
]
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import List, Dict, Any

from huggingface_hub import HfApi

API = HfApi()

def list_date_folder(repo_id: str, date: str) -> List[Dict[str, Any]]:
    """List files in a date prefix non-recursively to avoid pagination storms."""
    prefix = f"{date}/"
    entries = API.list_repo_tree(repo_id, recursive=False, path=prefix)
    out = []
    for e in entries:
        if e.type != "file":
            continue
        out.append({
            "path": e.path,
            "size": e.size or 0,
            # sha256 not provided by tree; we'll skip or fetch via /resolve/ ETag if needed.
        })
    return out

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()

    # Be nice to API after 429: single call only.
    items = list_date_folder(args.repo, args.date)
    with open(args.out, "w") as f:
        json.dump(items, f, indent=2)
    print(f"Wrote {len(items)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list-files.py
```

---

### 2) `lib/cdn_stream.py`

```python
import io
import json
import time
from typing import Dict, Iterator

import pyarrow.parquet as pq
import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_url(repo: str, path: str) -> str:
    return CDN_TEMPLATE.format(repo=repo, path=path)

def fetch_with_retry(url: str, max_retries: int = 5, backoff: float = 1.0) -> bytes:
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise RuntimeError("unreachable")

def project_row(raw: Dict[str, object]) -> Dict[str, str]:
    """
    Best-effort projection to {prompt, response}.
    Handles common schema variants seen in surrogate-1 mirrors.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def stream_parquet_from_cdn(repo: str, path: str) -> Iterator[Dict[str, str]]:
    data = fetch_with_retry(cdn_url(repo, path))
    table = pq.read_table(io.BytesIO(data))
    for batch in table.to_batches(max_chunksize=1000):
        cols = {name: batch.column(name).to_pylist() for name in batch.schema.names}
        n = len(next(iter(cols.values()))) if cols else 0
        for i in range(n):
            raw = {k: v[i] for k, v in cols.items()}
            yield project_row(raw)

def stream_jsonl_from_cdn(repo: str, path: str) -> Iterator[Dict[str, str]]:
    data = fetch_with_retry(cdn_url(repo, path))
    for line in io.BytesIO(data).read().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        yield project_row(raw)

def stream_from_cdn(repo: str, path: str) -> Iterator[Dict[str, str]]:
    if path.endswith(".parquet"):
        yield from stream_parquet_from_cdn(repo, path)
    elif path.endswith(".jsonl"):
        yield from stream_jsonl_from_cdn(repo, path)
    else:
        raise ValueError(f"Unsupported file type: {path}")
```

---

### 3) `requirements-cdn.txt`

```
requests>=2.31.0
pyarrow>=14.0.0
```

Update `requirements.txt` to include or reference CDN deps and pin versions used in CI.

---

### 4) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Usage:
#   FILE_LIST=file-list.json HF_TOKEN=hf_xxx ./bin/dataset-enrich.sh
# If FILE_LIST is set, uses CDN-only streaming (no HF API during ingest).

set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
SHARD=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
OUT_DIR="batches/public-merged/${DATE}"
mkdir -p "${OUT_DIR}"
OUT_FILE="${OUT_DIR}/shard${SHARD}-$(date -u +%H%M%S).jsonl"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# Deterministic shard assignment by slug hash
slug_hash() {
  echo -n "$1" | sha256sum | awk '{print "0x" substr($1,1,8)}'
}

belongs_to_shard() {
  local h=$(slug_hash "$1")
  local shard=$(( h % TOTAL_SHARDS ))
  [[ $shard -eq $SHARD ]]
}

# ---- CDN mode (preferred) ----
if [[ -n "${FILE_LIST:-}" && -f "$FILE_LIST" ]]; then
  log "CDN mode: using file list $FILE_LIST"
  python3 - <<'PY' 2> >(tee -a "${OUT_FILE}.err" >&2) | sort -u > "${OUT_FILE}"
import json
import os
import sys

from lib.cdn_stream import stream_from_cdn

repo = os.environ["REPO"]
file_list = os.environ["FILE_LIST"]

def slug_for(path):
    base = os.path.basename(path)
    return os.path.splitext(base)[0]

with open(file_list) as f:
    files = json.load(f)

for item in files:
    path = item["path"]
    if not belongs_to_shard(slug_for(path)):
        continue

