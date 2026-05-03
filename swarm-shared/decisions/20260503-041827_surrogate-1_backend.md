# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `bin/worker.py` — single-file, manifest-driven worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` (matrix) and a date folder (via `--date` or env).
   - Uses one HF API call (`list_repo_tree`) to list the target date folder, saves `manifest.json`.
   - Downloads only assigned shard files via **CDN bypass** (`resolve/main/...` with no auth).
   - Projects heterogeneous files to `{prompt, response}` at parse time (avoids `load_dataset`/pyarrow CastError).
   - Deduplicates via centralized SQLite md5 store (`lib/dedup.py`).
   - Streams output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
   - Exits 0 on success, non-zero on failure (GitHub Actions friendly).

2. Update `bin/dataset-enrich.sh` → thin wrapper that:
   - Sets `#!/usr/bin/env bash`, `set -euo pipefail`.
   - Exports `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`.
   - Invokes `python3 bin/worker.py` with matrix args.

3. Update `.github/workflows/ingest.yml` to:
   - Pass `date` (default: today) and matrix `shard_id`/`total_shards`.
   - Use `actions/setup-python` (3.10+).
   - Cache pip deps for speed.

4. Add minimal `requirements.txt` update (if missing): `requests`, `tqdm`, `pyarrow` (for parquet fallback).

---

### Code Snippets

#### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python bin/worker.py --repo axentx/surrogate-1-training-pairs --date 2026-05-03
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from tqdm import tqdm

HF_API = "https://huggingface.co/api"
CDN = "https://huggingface.co/datasets"
RETRY_WAIT = 360  # on 429

# ── dedup store ──────────────────────────────────────────────────────────────

def _get_db_path() -> Path:
    # Keep existing convention: central store on HF Space would be /data/dedup.db
    # For GitHub runners we use a local file (ephemeral). Cross-run dedup remains
    # best-effort; source-of-truth is the HF Space.
    return Path(os.getenv("DEDUP_DB", "dedup.db"))

def already_seen(digest: str) -> bool:
    import sqlite3

    db = _get_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
        cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (digest,))
        return cur.fetchone() is not None

def mark_seen(digest: str) -> None:
    import sqlite3

    db = _get_db_path()
    with sqlite3.connect(str(db)) as conn:
        conn.execute("INSERT OR IGNORE INTO seen (md5) VALUES (?)", (digest,))

# ── HF helpers ───────────────────────────────────────────────────────────────

def list_date_folder(repo: str, date: str) -> List[Dict[str, Any]]:
    """Single API call to list files in {date}/ (non-recursive)."""
    url = f"{HF_API}/datasets/{repo}/tree/main/{date}"
    r = requests.get(url, timeout=30)
    if r.status_code == 429:
        print(f"Rate limited. Waiting {RETRY_WAIT}s", file=sys.stderr)
        time.sleep(RETRY_WAIT)
        return list_date_folder(repo, date)
    r.raise_for_status()
    return r.json()

def cdn_download(repo: str, path: str, dest: Path) -> None:
    """Download via CDN (no auth)."""
    url = f"{CDN}/{repo}/resolve/main/{path}"
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

# ── projection (schema-agnostic) ────────────────────────────────────────────

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Best-effort projection to {prompt, response}.
    Supports common patterns seen in surrogate-1 sources.
    """
    # If already correct shape, return
    if "prompt" in raw and "response" in raw:
        return {"prompt": str(raw["prompt"]).strip(), "response": str(raw["response"]).strip()}

    # Common aliases
    prompt_keys = {"prompt", "input", "question", "instruction", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    found_prompt = None
    found_response = None
    for k in raw:
        ks = k.strip().lower()
        if ks in prompt_keys:
            found_prompt = raw[k]
        if ks in response_keys:
            found_response = raw[k]

    if found_prompt is not None and found_response is not None:
        return {"prompt": str(found_prompt).strip(), "response": str(found_response).strip()}

    # Fallback: first/second text-like fields
    text_fields = [v for v in raw.values() if isinstance(v, str) and len(v.strip()) > 10]
    if len(text_fields) >= 2:
        return {"prompt": text_fields[0].strip(), "response": text_fields[1].strip()}

    # Last resort
    return {"prompt": json.dumps(raw, ensure_ascii=False), "response": ""}

# ── worker main ──────────────────────────────────────────────────────────────

def run_worker(repo: str, date: str, shard_id: int, total_shards: int, out_dir: Path) -> None:
    items = list_date_folder(repo, date)
    files = [it for it in items if it.get("type") == "file"]
    if not files:
        print(f"No files found in {repo}/main/{date}/", file=sys.stderr)
        return

    # Deterministic shard assignment by filename slug
    def slug_hash(fn: str) -> int:
        return int(hashlib.md5(fn.encode()).hexdigest(), 16)

    assigned = [f for f in files if slug_hash(f["path"]) % total_shards == shard_id]
    print(f"Shard {shard_id}/{total_shards}: processing {len(assigned)} files")

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    written = 0
    skipped_dup = 0

    with open(out_path, "w", encoding="utf-8") as out_f:
        for meta in tqdm(assigned, desc="Downloading"):
            path = meta["path"]
            try:
                # Download via CDN (no HF API auth)
                tmp = out_dir / f".tmp_{os.getpid()}.download"
                cdn_download(repo, path, tmp)

                # Parse per known formats (surrogate-1 sources)
                content = tmp.read_bytes()
                records: Iterable[Dict[str, Any]]

                if path.endswith(".jsonl"):
                    records = (json.loads(l) for l in content.decode().splitlines() if l.strip())
                elif path.endswith(".json"):
                    obj = json.loads(content)
                    records = obj if isinstance(obj, list) else [obj]
                elif path.endswith((".parquet",
