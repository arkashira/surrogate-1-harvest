# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (env)
- On first run (or when `manifest.json` missing): uses HF API **once** from the orchestrator/Mac to `list_repo_tree(path=..., recursive=False)` for the target date folder, saves `manifest.json`
- Each shard loads the manifest, keeps only entries where `hash(filename) % SHARD_TOTAL == SHARD_ID`
- Downloads via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, no API rate limits during training
- Streams each file, projects to `{prompt,response}` only, computes content md5 for dedup against central SQLite store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with newline JSON records
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)

### Why this wins
- Eliminates `load_dataset(streaming=True)` schema explosions
- Bypasses HF API rate limits during data load (CDN tier)
- Keeps HF API usage to a single `list_repo_tree` call per date
- Deterministic sharding prevents commit collisions
- Reuses existing central dedup store pattern

---

## Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 dataset-enrich worker (CDN-bypass, manifest-driven)

Usage (GitHub Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          : 0..15
  SHARD_TOTAL       : 16 (default)
  DATE              : YYYY-MM-DD folder to ingest
  HF_TOKEN          : write token for axentx/surrogate-1-training-pairs
  DATASET_REPO      : default axentx/surrogate-1-training-pairs
  MANIFEST_PATH     : default manifest.json (cached repo file list)
  CENTRAL_DB_URL    : sqlite:///dedup.db (or remote)
"""

import os
import sys
import json
import hashlib
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download, login

# ---- config ----
HF_TOKEN = os.getenv("HF_TOKEN")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest.json")
CENTRAL_DB_URL = os.getenv("CENTRAL_DB_URL", "sqlite:///dedup.db")
API_RETRY_WAIT = 360  # seconds after 429
# ----

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

login(token=HF_TOKEN)
api = HfApi(token=HF_TOKEN)

# ---- helpers ----
def deterministic_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def shard_filter(filename: str) -> bool:
    return deterministic_hash(filename) % SHARD_TOTAL == SHARD_ID

def build_manifest() -> List[str]:
    """Single API call: list top-level or date folder contents (non-recursive)."""
    # If DATE folder exists, list it; else list repo root
    try:
        items = api.list_repo_tree(
            repo_id=DATASET_REPO,
            path=DATE,
            repo_type="dataset",
            recursive=False,
        )
        # items are dict-like; extract path
        paths = [it["path"] for it in items if it.get("type") == "file"]
    except Exception as e:
        # fallback: list root and filter by DATE prefix
        print(f"WARN: listing {DATE} failed ({e}), falling back to root scan", file=sys.stderr)
        items = api.list_repo_tree(
            repo_id=DATASET_REPO,
            path="",
            repo_type="dataset",
            recursive=False,
        )
        paths = [it["path"] for it in items if it.get("type") == "file" and it["path"].startswith(f"{DATE}/")]
    return sorted(paths)

def save_manifest(paths: List[str]) -> None:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": DATE, "paths": paths, "generated_at": datetime.utcnow().isoformat()}, f)

def load_manifest() -> List[str]:
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("paths", [])
    return []

def get_file_list() -> List[str]:
    paths = load_manifest()
    if not paths:
        print("Building manifest (single API call)...")
        paths = build_manifest()
        save_manifest(paths)
        print(f"Manifest saved: {len(paths)} files")
    return paths

def cdn_download_url(repo: str, path: str) -> str:
    # Public CDN URL — no auth header required
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Heuristic projection to {prompt, response}.
    Adjust per observed schema; keep minimal.
    """
    # Common patterns seen in surrogate-1 training pairs
    prompt = None
    response = None

    # direct fields
    if "prompt" in raw and "response" in raw:
        prompt, response = raw["prompt"], raw["response"]
    elif "instruction" in raw and "output" in raw:
        prompt, response = raw["instruction"], raw["output"]
    elif "input" in raw and "output" in raw:
        prompt, response = raw["input"], raw["output"]
    elif "question" in raw and "answer" in raw:
        prompt, response = raw["question"], raw["answer"]
    elif "text" in raw:
        # fallback: split by last newline or separator if needed
        prompt = raw["text"]
        response = ""

    if prompt is None:
        prompt = ""
    if response is None:
        response = ""

    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def init_dedup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(CENTRAL_DB_URL.replace("sqlite:///", ""))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_md5 (
            md5 TEXT PRIMARY KEY,
            inserted_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5 = ?", (md5,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    try:
        conn.execute("INSERT INTO seen_md5 (md5) VALUES (?)", (md5,))
    except sqlite3.IntegrityError:
        pass  # race ok

# ---- worker ----
def run_worker() -> None:
    print(f"Worker shard {SHARD_ID}/{SHARD_TOTAL} | date={DATE}")

    file_list = get_file_list()
    my_files = [p for p in file_list if shard_filter(p)]
    print(f"Shard files: {len(my_files)}")

    if not my_files:
        print("No files assigned to this shard. Exiting.")
        return

    conn = init_dedup_db()
    out_dir = Path(f"batches/public-merged/{DATE
