# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses **manifest-first strategy**: single API call to `list_repo_tree` for the target `DATE` folder → saves `manifest.json` → Lightning training uses CDN-only fetches (zero API calls during data load)
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — no Authorization header, avoids 429 rate limits
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on fatal failure
- **Retry/backoff for 429** (wait 360s) and **commit-cap spreading** across sibling repos (hash slug → pick repo)

### Steps (≤2h)
1. Create `bin/dataset-enrich.py` (main worker)
2. Update `.github/workflows/ingest.yml` to invoke via `python bin/dataset-enrich.py` with matrix env
3. Add `requirements-dev.txt` (if needed) or update `requirements.txt` with `requests`
4. Quick smoke test via `gh workflow run` or local invocation

---

## Code

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
    HF_TOKEN=hf_xxx \
    python bin/dataset-enrich.py

Environment:
  SHARD_ID          - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE              - date folder in dataset repo (e.g. 2026-05-03)
  HF_TOKEN          - HuggingFace write token
  REPO_ID           - dataset repo (default: axentx/surrogate-1-training-pairs)
  DEDUP_DB_PATH     - path to central md5 sqlite store (default: lib/dedup.db)
  SIBLING_REPOS     - comma-separated list of repos for commit-cap spreading
"""

import os
import sys
import json
import hashlib
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ---------- config ----------
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
DEDUP_DB_PATH = os.getenv("DEDUP_DB_PATH", "lib/dedup.db")
SIBLING_REPOS = os.getenv("SIBLING_REPOS", "").split(",") if os.getenv("SIBLING_REPOS") else []

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
TS = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
OUT_DIR = Path(f"batches/public-merged/{DATE}")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"
MANIFEST_PATH = Path(f"manifest-{DATE}.json")
CDN_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

# ---------- dedup ----------
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_md5 ("
        "  md5 TEXT PRIMARY KEY,"
        "  inserted_at TEXT NOT NULL"
        ")"
    )
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5 = ?", (md5,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("INSERT INTO seen_md5 (md5, inserted_at) VALUES (?, ?)", (md5, now))
    except sqlite3.IntegrityError:
        pass  # race ok

# ---------- manifest ----------
def build_manifest(date_folder: str) -> List[str]:
    """
    Single API call: list top-level files in date folder (non-recursive).
    Returns list of repo-relative paths.
    """
    try:
        tree = list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"ERROR: failed to list repo tree for {date_folder}: {exc}", file=sys.stderr)
        return []

    # tree items have .path
    paths = [item.path for item in tree if getattr(item, "path", "")]
    # Keep only files we expect (parquet/jsonl/json)
    allowed_ext = {".parquet", ".jsonl", ".json"}
    paths = [p for p in paths if Path(p).suffix.lower() in allowed_ext]
    paths.sort()
    return paths

def save_manifest(paths: List[str], path: Path) -> None:
    path.write_text(json.dumps({"date": DATE, "paths": paths}, indent=2))

def load_manifest(path: Path) -> Optional[List[str]]:
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return data.get("paths", [])

# ---------- shard assignment ----------
def assign_shard(path: str, shard_id: int, shard_total: int) -> bool:
    """Deterministic shard assignment by slug hash."""
    slug = Path(path).stem
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return (h % shard_total) == shard_id

# ---------- commit-cap spreading ----------
def pick_repo_for_slug(slug: str) -> str:
    """Pick repo from SIBLING_REPOS based on slug hash."""
    if not SIBLING_REPOS:
        return REPO_ID
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLING_REPOS[h % len(SIBLING_REPOS)]

# ---------- parsing ----------
def normalize_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Accepts common field names.
    """
    if not isinstance(obj, dict):
        return None

    prompt_keys = {"prompt", "instruction", "input", "question", "query"}
    response_keys = {"response", "output", "answer", "completion", "result"}

    prompt = None
    response = None

    for k, v in obj.items():
        if k in prompt_keys and isinstance(v, str) and v.strip():
            prompt = v.strip()
        if k in response_keys and isinstance(v, str) and v.strip():
            response = v.strip()

    # fallback: if only one text-like field exists, try to split by common separators
    if prompt is None or response is None:
        text_keys = [k for k, v in obj.items() if isinstance(v, str) and v.strip()]
        if len(text_keys) == 1:
            txt = obj[text_keys[0]].strip()
            # naive split; datasets should ideally conform
            sep_candidates = ["\n\n", "\n", "##", "###", "<｜end
