# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses **one** `list_repo_tree` call (per date folder) to enumerate parquet files, saves manifest JSON, then processes only the deterministic shard slice
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization header during data streaming (avoids 429 API limits)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central md5 store (`lib/dedup.py`) and writes to `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Reuses running Lightning Studio when available (saves quota)
- Returns exit code 0 on success, non-zero on fatal error (so GitHub Actions matrix fails fast)

---

### Steps (est. 90 min)
1. Create `bin/dataset-enrich.py` (60 min) — manifest + CDN download + shard routing + schema projection + dedup + upload
2. Update `.github/workflows/ingest.yml` to invoke via `python bin/dataset-enrich.py` with matrix env (10 min)
3. Make executable + quick smoke test (20 min)

---

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Behavior:
- Single list_repo_tree call for the DATE folder
- Saves file manifest to manifest-{SHARD_ID}.json
- Downloads via HF CDN (no auth header) to avoid API rate limits
- Projects each file to {prompt, response} only at parse time
- Dedups via lib/dedup.py (central md5 store)
- Writes shard output to batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl
- Deterministic shard assignment by slug-hash
- Reuses Lightning Studio session when available (no HF login if already authenticated)
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from huggingface_hub import HfApi, hf_hub_download, whoami

REPO_DATASET = "axentx/surrogate-1-training-pairs"

# ---------- auth: reuse studio or token ----------
def _get_api() -> HfApi:
    hf_token = os.getenv("HF_TOKEN", "")
    # If running in Lightning Studio, reuse existing session
    studio_token_path = Path.home() / ".cache" / "huggingface" / "token"
    if studio_token_path.exists():
        return HfApi()
    if not hf_token:
        print("[ERROR] HF_TOKEN is required when not in Lightning Studio", file=sys.stderr)
        sys.exit(1)
    return HfApi(token=hf_token)

API = _get_api()

# ---------- helpers ----------
def _hash_slug(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

def _shard_for(slug: str, total: int) -> int:
    return _hash_slug(slug) % total

def _now_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

def _date_folder(date: str) -> str:
    # Expects date in YYYY-MM-DD; stored as YYYY-MM-DD under public-merged/
    return date

def _parquet_files_for_date(date: str) -> List[str]:
    """
    Single list_repo_tree call for the date folder.
    Returns relative paths (within repo) to parquet files.
    """
    folder = f"public-merged/{_date_folder(date)}"
    try:
        tree = API.list_repo_tree(repo_id=REPO_DATASET, path=folder, recursive=False)
    except Exception as exc:
        # If folder doesn't exist yet, return empty list (first run)
        return []
    files = [item.path for item in tree if item.path.lower().endswith(".parquet")]
    return sorted(files)

def _cdn_download_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def _download_parquet(path: str, dest: Path) -> Path:
    """
    Prefer CDN download (no auth header) to bypass API rate limits.
    Falls back to hf_hub_download if CDN fails.
    """
    url = _cdn_download_url(REPO_DATASET, path)
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest
    except Exception:
        # fallback
        return Path(hf_hub_download(repo_id=REPO_DATASET, filename=path, local_dir=dest.parent))

def _project_to_pair(batch: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project a single row-like record to {prompt, response}.
    Tolerates mixed schemas; returns None if unusable.
    """
    prompt = batch.get("prompt") or batch.get("input") or batch.get("question")
    response = batch.get("response") or batch.get("output") or batch.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def _read_parquet_pairs(path: Path):
    """
    Stream rows from parquet and yield projected pairs.
    """
    try:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=1024, columns=None):
            table = pa.Table.from_batches([batch])
            df = table.to_pylist()
            for row in df:
                pair = _project_to_pair(row)
                if pair:
                    yield pair
    except Exception as exc:
        # If pyarrow fails on mixed schema, skip file and log
        print(f"[WARN] Failed to read {path}: {exc}", file=sys.stderr)
        return

# ---------- dedup bridge ----------
def _compute_md5_for_pair(pair: Dict[str, str]) -> str:
    payload = f"{pair['prompt']}\n{pair['response']}".encode()
    return hashlib.md5(payload).hexdigest()

def _is_duplicate(md5: str) -> bool:
    # Use lib/dedup.py as central store (import if available)
    try:
        # dedup.py expected to expose: is_duplicate(md5: str) -> bool and mark(md5: str) -> None
        from lib.dedup import is_duplicate, mark
        if is_duplicate(md5):
            return True
        mark(md5)
        return False
    except Exception:
        # Fallback: local sqlite in cwd if lib/dedup unavailable
        import sqlite3
        db = Path("dedup_local.db")
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE IF NOT EXISTS md5s (md5 TEXT PRIMARY KEY)")
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM md5s WHERE md5=?", (md5,))
        if cur.fetchone():
            conn.close()
            return True
        conn.execute("INSERT INTO md5s (md5) VALUES (?)", (md5,))
        conn.commit()
        conn.close()
        return False

# ---------- main worker ----------
def main() -> int:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))

    print(f"[INFO] Shard {shard_id}/{shard_total} | DATE={date}")

    # 1) enumerate files once
    files = _parquet_files_for_date(date)
   
