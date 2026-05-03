# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Single API call to `list_repo_tree(path, recursive=False)` for one date folder → save `manifest.json`.
   - Worker loads manifest and downloads every file via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
   - Stream-parse each file, project to `{prompt, response}` only, compute md5, emit normalized JSONL.
   - Deterministic shard assignment via `hash(slug) % 16 == SHARD_ID`.
   - Central dedup via thread-safe `lib/dedup.py` (SQLite md5 store).
   - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **`lib/dedup.py`** (unchanged API, ensure thread-safe writes)
   - Add context manager for safe concurrent access from multiple workers/processes.

3. **`requirements.txt`**
   - Add `requests` (for CDN downloads), keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

4. **`.github/workflows/ingest.yml`**
   - Update matrix step to run `python bin/dataset-enrich.py` instead of shell script.
   - Pass `SHARD_ID`, `HF_TOKEN`, `DATE_FOLDER` (or default to today) as env vars.

### Why this matters
- **Rate-limit safety**: CDN downloads avoid 429s; single `list_repo_tree` call per shard (or once per workflow) stays under 1000 req/5min.
- **Schema safety**: Project to `{prompt, response}` at parse time — prevents `pyarrow.CastError` from heterogeneous files.
- **Deterministic sharding**: `hash(slug) % 16` ensures no collisions across shards and stable assignment across reruns.
- **Training readiness**: Manifest can be reused by training scripts for CDN-only fetches (zero API calls during training).

---

## Code Snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-first, CDN-bypass enrichment worker for surrogate-1.
Usage (via GitHub Actions matrix):
  SHARD_ID=0 python bin/dataset-enrich.py
Env:
  HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
  DATE_FOLDER       - optional; defaults to today YYYY-MM-DD
  REPO_OWNER        - default: axentx
  REPO_NAME         - default: surrogate-1-training-pairs
  SHARD_ID          - 0..15 (required)
"""
import io
import json
import hashlib
import datetime
import sqlite3
import sys
from pathlib import Path
from typing import List, Dict, Any

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from huggingface_hub import HfApi

# ── config ──────────────────────────────────────────────────────────────
REPO_OWNER = os.getenv("REPO_OWNER", "axentx")
REPO_NAME = os.getenv("REPO_NAME", "surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", -1))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())

if SHARD_ID < 0 or SHARD_ID > 15:
    print("ERROR: SHARD_ID must be 0..15", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
BASE_DATASET_REPO = f"{REPO_OWNER}/{REPO_NAME}"
MANIFEST_PATH = Path("manifest.json")
OUT_DIR = Path(f"batches/public-merged/{DATE_FOLDER}")
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── dedup (central sqlite) ─────────────────────────────────────────────
DEDUP_DB = Path("dedup_hashes.db")

def init_dedup() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DEDUP_DB), timeout=30.0)
    conn.execute("CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM hashes WHERE md5=?", (md5,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    try:
        conn.execute("INSERT INTO hashes (md5) VALUES (?)", (md5,))
    except sqlite3.IntegrityError:
        pass  # race ok

# ── manifest helpers ───────────────────────────────────────────────────
def list_date_files() -> List[Dict[str, Any]]:
    """Single API call: non-recursive tree for the date folder."""
    try:
        tree = API.list_repo_tree(
            repo_id=BASE_DATASET_REPO,
            path=DATE_FOLDER,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"ERROR listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        files.append(
            {
                "path": entry.path,
                "size": getattr(entry, "size", None),
            }
        )
    return files

def save_manifest(files: List[Dict[str, Any]]) -> None:
    with MANIFEST_PATH.open("w") as f:
        json.dump({"date": DATE_FOLDER, "files": files}, f)

def load_manifest() -> List[Dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    with MANIFEST_PATH.open() as f:
        data = json.load(f)
    return data.get("files", [])

# ── shard assignment ───────────────────────────────────────────────────
def slug_from_path(path: str) -> str:
    return Path(path).stem

def shard_for_slug(slug: str) -> int:
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % 16

# ── CDN download & parse ──────────────────────────────────────────────
CDN_BASE = f"https://huggingface.co/datasets/{BASE_DATASET_REPO}/resolve/main"

def download_via_cdn(repo_path: str) -> bytes:
    url = f"{CDN_BASE}/{repo_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def project_to_pair(raw_bytes: bytes, path: str) -> List[Dict[str, str]]:
    """
    Lightweight projection to {prompt,response}. Supports .jsonl and .parquet.
    Drops extra fields to avoid schema mismatches.
    """
    suffix = Path(path).suffix.lower()
    pairs = []

    if suffix == ".jsonl":
        for line in io.TextIOWrapper(io.BytesIO(raw_bytes), encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt is None or response is None:
                continue
            pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    if suffix == ".parquet":
        table = pq.read_table(io.BytesIO(raw_bytes))
        cols = set(table.column_names)
        prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
        response_col = next((c for c in ("response", "output", "answer") if c in cols), None)
