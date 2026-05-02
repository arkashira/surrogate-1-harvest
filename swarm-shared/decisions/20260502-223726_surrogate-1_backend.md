# surrogate-1 / backend

## Final Implementation Plan (≤2 h)

**Highest-value improvement**  
Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we will do (90 min target)

1. **`bin/list-files.py`** — one-time script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path, size, sha256, CDN URL). Embed this list in training/shard scripts so workers perform **zero** HF API calls during data loading.
2. **`bin/dataset-enrich.sh`** — accept an optional `FILE_LIST_JSON`. When provided, stream via CDN (`resolve/main/...`) with `curl`/`wget`; keep `hf_hub_download` fallback for private repos or CDN failures.
3. **`surrogate_1/cdn_worker.py`** — use the file list to stream each parquet/jsonl via CDN, project to `{prompt,response}` only, dedup via central SQLite, and write shards to `public-merged/<date>/`.

Total time: ~90 min (implementation + per-shard smoke test).

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/list-files.py --date 2026-05-02 > file-list.json

Output keys:
  - path: repo path
  - size: bytes
  - sha256: if available in tree
  - url: CDN URL (no auth)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"

def list_date_folder(date_str: str, api: HfApi) -> list[dict]:
    path = date_str
    try:
        tree = api.list_repo_tree(
            repo_id=REPO_ID,
            path=path,
            recursive=True,
            repo_type="dataset",
        )
    except Exception as exc:
        sys.stderr.write(f"Failed to list {path}: {exc}\n")
        return []

    out = []
    for node in tree:
        if node.type != "file":
            continue
        out.append(
            {
                "path": node.path,
                "size": node.size or 0,
                "sha256": getattr(node, "oid", None),
                "url": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{node.path}",
            }
        )
    return out

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    files = list_date_folder(args.date, api)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": args.date,
        "repo": REPO_ID,
        "count": len(files),
        "files": files,
    }
    json.dump(meta, sys.stdout, indent=2)
    sys.stdout.write("\n")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list-files.py
```

---

### 2) `bin/dataset-enrich.sh` (CDN-aware mode)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Usage:
#   # HF datasets mode (existing)
#   HF_TOKEN=... python -m surrogate_1.legacy_worker ...
#
#   # CDN mode (new)
#   CDN_MODE=1 FILE_LIST_JSON=file-list.json python -m surrogate_1.cdn_worker ...

set -euo pipefail
SHELL=/bin/bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export HF_HOME="${HF_HOME:-$REPO_ROOT/.hf_cache}"
mkdir -p "$HF_HOME"

# Dedup store (central)
export DEDUP_DB_PATH="${DEDUP_DB_PATH:-$REPO_ROOT/lib/dedup.db}"

# Mode selection
if [[ "${CDN_MODE:-0}" == "1" && -n "${FILE_LIST_JSON:-}" && -f "$FILE_LIST_JSON" ]]; then
  echo "INFO: CDN mode enabled, file list: $FILE_LIST_JSON"
  exec python -m surrogate_1.cdn_worker --file-list "$FILE_LIST_JSON" "$@"
else
  echo "INFO: HF datasets/legacy mode"
  exec python -m surrogate_1.legacy_worker "$@"
fi
```

---

### 3) `surrogate_1/cdn_worker.py`

```python
#!/usr/bin/env python3
"""
CDN-only worker.

- Reads file-list.json produced by bin/list-files.py
- Streams each parquet/jsonl via CDN URL
- Projects to {prompt, response}
- Dedups via central SQLite store (lib/dedup.py)
- Outputs shards to public-merged/<date>/shard<N>-<HHMMSS>.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

BATCH_SIZE = 500
REPO = "axentx/surrogate-1-training-pairs"

def row_to_pair(row: Dict[str, Any]) -> Dict[str, str]:
    prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
    response = row.get("response") or row.get("output") or row.get("answer") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def hash_pair(pair: Dict[str, str]) -> str:
    payload = f"{pair['prompt']}\n{pair['response']}".encode()
    return hashlib.md5(payload).hexdigest()

def stream_parquet_cdn(url: str) -> Iterable[Dict[str, Any]]:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        fname = f.name
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(fname, "wb") as f:
            f.write(resp.content)
        table = pq.read_table(fname)
        for batch in table.to_batches(max_chunksize=8192):
            for row in batch.to_pylist():
                yield row
    finally:
        Path(fname).unlink(missing_ok=True)

def stream_jsonl_cdn(url: str) -> Iterable[Dict[str, Any]]:
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if line:
            yield json.loads(line)

class DedupStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY, ts REAL)"
        )
        self.conn.commit()

    def exists(self, md5: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
        return cur.fetchone() is not None

    def add(self, md5: str) -> None:
        try:
            self.conn.execute("INSERT INTO seen (md5, ts) VALUES (?, ?)", (md5, time.time()))
        except sqlite3.IntegrityError:
            pass

    def commit(self):
        self.conn.commit()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()

    with open(args.file_list) as f:
        meta = json.load
