# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Shard assignment by `hash(slug) % SHARD_TOTAL` (consistent across runs)
- Per-file CDN download via `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{DATE}/{file}` (no Authorization header → bypasses API rate limits)
- Stream-parse each file, project to `{prompt, response}` only, compute md5 for dedup
- Local SQLite dedup (fallback to central store if available) to avoid re-upload of known hashes
- Append shard output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Commit via HF API (single commit per shard per run)

### Why this is the highest-value incremental improvement
- **Eliminates HF API rate-limit risk** during data loading by using CDN-only fetches (the key 2026-04-29 insight)
- **Deterministic sharding** prevents cross-run collisions and enables safe retries
- **Single tree call** respects HF API limits (no recursive `list_repo_files`)
- **Schema-safe projection** avoids pyarrow CastError on mixed schemas
- **Self-contained Python worker** replaces brittle shell script, easier to test and extend

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          - worker index [0..SHARD_TOTAL-1]
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE              - dataset subfolder date (e.g. 2026-04-29)
  HF_TOKEN          - HuggingFace write token
  REPO_ID           - dataset repo (default axentx/surrogate-1-training-pairs)
  DEDUP_DB_PATH     - local SQLite dedup db (default ./dedup.db)
  DRY_RUN           - if set, skip upload and print actions
  OUTPUT_DIR        - output root (default ./batches/public-merged)
"""

import os
import sys
import json
import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple, Optional

import requests
from huggingface_hub import HfApi, list_repo_tree

# ---------- config ----------
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "")
DEDUP_DB_PATH = os.getenv("DEDUP_DB_PATH", "./dedup.db")
DRY_RUN = bool(os.getenv("DRY_RUN"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./batches/public-merged"))

if not DATE:
    print("ERROR: DATE is required (YYYY-MM-DD)", file=sys.stderr)
    sys.exit(1)

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    print(f"ERROR: SHARD_ID must be in [0..{SHARD_TOTAL-1}]", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
DATE_FOLDER = DATE  # subfolder under repo root

# ---------- dedup ----------
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("CREATE TABLE IF NOT EXISTS seen_md5 (md5 TEXT PRIMARY KEY, ts TEXT)")
    conn.commit()
    return conn

def is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5=?", (md5,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("INSERT INTO seen_md5 (md5, ts) VALUES (?, ?)", (md5, ts))
    except sqlite3.IntegrityError:
        pass  # race ok

# ---------- file listing ----------
def shard_files(date_folder: str) -> list[str]:
    """
    List files in repo subfolder and deterministically assign to shards.
    Uses list_repo_tree (non-recursive) to avoid recursive pagination.
    """
    print(f"Listing repo tree: {REPO_ID}/{date_folder}")
    try:
        tree = list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    except Exception as exc:
        print(f"ERROR listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = [item.rfilename for item in tree if item.type == "file"]
    files.sort()

    assigned = []
    for f in files:
        # deterministic shard by slug hash
        slug = Path(f).stem  # filename without extension as slug
        h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
        if h % SHARD_TOTAL == SHARD_ID:
            assigned.append(f)

    print(f"Assigned {len(assigned)}/{len(files)} files to shard {SHARD_ID}")
    return assigned

# ---------- CDN download + parse ----------
def cdn_url(repo_id: str, date_folder: str, filename: str) -> str:
    # Public CDN URL — no Authorization header required
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{date_folder}/{filename}"

def stream_cdn_lines(url: str, chunk_size: int = 8192) -> Iterator[bytes]:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk

def detect_format_and_project(raw_sample: dict) -> Optional[Tuple[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Supports common keys seen in surrogate-1 training pairs.
    """
    if not isinstance(raw_sample, dict):
        return None

    # Normalize keys to lowercase for robustness
    low = {k.lower(): v for k, v in raw_sample.items()}

    prompt = None
    response = None

    # Common prompt keys
    for k in ("prompt", "instruction", "input", "question", "text"):
        if k in low and isinstance(low[k], str) and low[k].strip():
            prompt = low[k].strip()
            break

    # Common response keys
    for k in ("response", "completion", "output", "answer", "generation"):
        if k in low and isinstance(low[k], str) and low[k].strip():
            response = low[k].strip()
            break

    # Fallback: if only one text-like field, split by separator
    if prompt is None or response is None:
        for k in ("text", "content", "conversation"):
            if k in low and isinstance(low[k], str):
                parts = low[k].split("\n\n", 1)
                if len(parts) == 2:
                    if prompt is None:
                        prompt = parts[0].strip()
                    if response is None:
                        response = parts[1].strip()
                    break

    if prompt is None or response is None:
        return None
    return prompt, response

def parse_file(date_folder: str, filename: str) -> Iterator[Tuple[str, str, str]]:
    """
    Yield (prompt, response, md5) for each valid sample in file.
    Supports JSONL and JSON array-of-objects
