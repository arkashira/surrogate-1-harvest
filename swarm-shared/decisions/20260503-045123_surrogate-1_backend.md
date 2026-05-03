# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Changes

1. **Add `bin/gen_manifest.py`** — run once per day (Mac or workflow)  
   - Uses one HF API call per date folder (`list_repo_tree` non-recursive)  
   - Emits `batches/public-merged/<date>/manifest.json` with `{"date": "...", "files": [{"path": "...", "size": ...}]}`  
   - Commits manifest to repo (or uploads as workflow artifact) so workers never call HF API again during shard runs.

2. **Add `bin/worker.py`** — manifest-driven, CDN-only shard worker  
   - Accepts `SHARD_ID`, `TOTAL_SHARDS` (matrix) and `MANIFEST_PATH` (or date) via env  
   - Reads local `manifest.json`; downloads each parquet via raw CDN URL (`resolve/main/...`) — zero auth, bypasses `/api/` rate limits  
   - Projects to `{prompt, response}` only at parse time; drops extra columns to avoid mixed-schema `CastError`  
   - Uses deterministic path-based shard assignment so workers can run in parallel without overlap  
   - Produces `shard-<N>-<YYYYmmddHHMMSS>.jsonl` in `batches/public-merged/<date>/`  
   - Uses central SQLite dedup store (`dedup_hashes.db`) for cross-run dedup (same as existing)

3. **Update `bin/dataset-enrich.sh`** — thin, safe wrapper  
   - `#!/usr/bin/env bash`, `set -euo pipefail`  
   - Exports `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`  
   - Invokes `python bin/worker.py "$@"` so cron/actions can `bash bin/dataset-enrich.sh` safely

4. **Update `.github/workflows/ingest.yml`**  
   - Add a non-matrix “manifest” job (runs once) that calls `bin/gen_manifest.py` and uploads/saves manifest as artifact or commits it  
   - Matrix job uses `bash` and passes `SHARD_ID`/`TOTAL_SHARDS` unchanged; each matrix entry runs `bin/worker.py` reading the committed/artifact manifest

5. **Add/confirm `requirements.txt`**: `requests`, `pyarrow`, `pandas`, `tqdm`

---

### Code snippets

#### `bin/gen_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder to avoid HF API calls during shard runs.
Usage:
  HF_TOKEN=... python bin/gen_manifest.py 2024-06-01
Writes: batches/public-merged/2024-06-01/manifest.json
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path

import requests

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

def list_date_folders():
    url = f"https://huggingface.co/api/datasets/{HF_REPO}/tree"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        sys.exit("HF API 429 — retry later")
    r.raise_for_status()
    items = r.json()
    folders = [item["path"] for item in items if item["type"] == "directory"]
    folders = [f for f in folders if len(f.split("-")) == 3 and len(f) == 10]
    folders.sort()
    return folders

def list_parquet_files(date_folder: str):
    url = f"https://huggingface.co/api/datasets/{HF_REPO}/tree"
    r = requests.get(url, params={"path": date_folder}, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        sys.exit("HF API 429 — retry later")
    r.raise_for_status()
    items = r.json()
    files = [
        {"path": item["path"], "size": item.get("size", 0)}
        for item in items
        if item["type"] == "file" and item["path"].endswith(".parquet")
    ]
    files.sort(key=lambda x: x["path"])
    return files

def main():
    if len(sys.argv) < 2:
        # default to latest
        folders = list_date_folders()
        if not folders:
            sys.exit("No date folders found")
        date_folder = folders[-1]
    else:
        date_folder = sys.argv[1]

    files = list_parquet_files(date_folder)
    out_dir = Path("batches/public-merged") / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"date": date_folder, "files": files}
    out_path = out_dir / "manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path}")

if __name__ == "__main__":
    main()
```

#### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass worker for surrogate-1 public dataset ingestion.

Environment:
  SHARD_ID     (int) 0..TOTAL_SHARDS-1
  TOTAL_SHARDS (int) default 16
  HF_TOKEN     (str) optional (unused for CDN downloads)
  HF_REPO      (str) default axentx/surrogate-1-training-pairs
  CDN_BASE     (str) default https://huggingface.co/datasets
  MANIFEST_PATH (str) optional path to manifest.json; else inferred from date
"""

import os
import sys
import json
import hashlib
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Iterator, Tuple

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from tqdm import tqdm

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
CDN_BASE = os.getenv("CDN_BASE", "https://huggingface.co/datasets")

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))

REPO_ROOT = Path(__file__).parent.parent.parent
DB_PATH = REPO_ROOT / "dedup_hashes.db"

def cdn_url(path: str) -> str:
    return f"{CDN_BASE}/{HF_REPO}/resolve/main/{path}"

def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def init_dedup() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS hashes (hash TEXT PRIMARY KEY, ts TEXT)")
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, h: str) -> bool:
    cur = conn.execute("SELECT 1 FROM hashes WHERE hash=?", (h,))
    return cur.fetchone() is not None

def mark_duplicate(conn: sqlite3.Connection, h: str) -> None:
    try:
        conn.execute("INSERT INTO hashes (hash, ts) VALUES (?, ?)", (h, dt.datetime.utcnow().isoformat()))
    except sqlite3.IntegrityError:
        pass

def rows_from_parquet_cdn(path: str) -> Iterator[Tuple[str, dict]]:
    """
    Download parquet via CDN, project to {prompt,response}, yield (hash, row).
    Avoids mixed-schema CastErrors by reading only expected columns when possible.
    """
    url = cdn_url(path)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    data = r.content

    try:
        table = pq.read_table(pa.BufferReader(data), columns=["prompt", "response"])
    except (pa.ArrowInvalid, KeyError):
        table = pq.read_table(pa.BufferReader(data))
        if "prompt" not in table.column_names or "response" not in table.column_names:
            col_map = {}
            for c in table.column
