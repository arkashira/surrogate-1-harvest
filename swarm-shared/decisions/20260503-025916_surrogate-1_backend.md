# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list for the date folder
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL` (consistent across runs)
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during data load, no Authorization header (bypasses API rate limits)
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError from mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with deterministic filename; attribution encoded in filename only (no extra columns)
- Exits 0 on success, non-zero on fatal error; logs structured JSON for GH Actions

### Code changes

```python
#!/usr/bin/env python3
"""
bin/dataset-enrich.py
CDN-bypass, manifest-driven ingestion worker for surrogate-1.
Usage (GH Actions matrix):
  SHARD_ID=0..15 SHARD_TOTAL=16 DATE=2026-05-03 HF_TOKEN=... python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import datetime
import logging
import sqlite3
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

# --
# Config
# --
REPO_ID = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dataset-enrich")

# --
# Dedup (central md5 store)
# --
def _dedup_db_path() -> Path:
    return Path(__file__).parent / "lib" / ".dedup.sqlite"

def _ensure_dedup_table(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")

def is_duplicate(md5_hex: str) -> bool:
    db_path = _dedup_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_dedup_table(conn)
        cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5_hex,))
        exists = cur.fetchone() is not None
        if not exists:
            conn.execute("INSERT INTO seen (md5) VALUES (?)", (md5_hex,))
        return exists

# --
# Manifest + sharding
# --
def list_date_files(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    api = HfApi(token=HF_TOKEN)
    items = api.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    paths = []
    for it in items:
        p = it.path if hasattr(it, "path") else it.get("path")
        if p and not p.endswith("/"):
            paths.append(p)
    paths.sort()
    return paths

def shard_for(path: str) -> int:
    slug = Path(path).stem
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % SHARD_TOTAL

# --
# CDN download + projection
# --
def download_via_cdn(repo_path: str) -> bytes:
    url = f"{CDN_ROOT}/{repo_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def project_to_pair(raw_bytes: bytes, repo_path: str) -> Dict[str, str]:
    """
    Best-effort projection to {prompt,response} without loading full heterogeneous schema.
    Supports:
      - JSON/JSONL with prompt/response fields
      - Parquet via pyarrow (project only two columns)
    """
    import io

    # Try JSON/JSONL first
    try:
        data = json.loads(raw_bytes.decode())
        if isinstance(data, list):
            data = data[0] if data else {}
        if "prompt" in data and "response" in data:
            return {"prompt": str(data["prompt"]), "response": str(data["response"])}
    except Exception:
        pass

    # Try parquet
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(io.BytesIO(raw_bytes), columns=["prompt", "response"])
        if table.num_rows > 0:
            row = table.slice(0, 1).to_pydict()
            return {
                "prompt": str(row["prompt"][0]),
                "response": str(row["response"][0]),
            }
    except Exception:
        pass

    # Fallback: try JSONL lines
    try:
        text = raw_bytes.decode()
        for line in text.strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if "prompt" in obj and "response" in obj:
                return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}
    except Exception:
        pass

    raise ValueError(f"Cannot project {repo_path} to prompt/response")

# --
# Worker
# --
def run() -> None:
    if not HF_TOKEN:
        log.error("HF_TOKEN is required")
        sys.exit(1)

    log.info("Starting shard=%s/%s date=%s", SHARD_ID, SHARD_TOTAL, DATE)

    try:
        files = list_date_files(DATE)
    except Exception as exc:
        log.error("Failed to list repo tree: %s", exc)
        sys.exit(1)

    target_files = [f for f in files if shard_for(f) == SHARD_ID]
    log.info("Found %d files total, %d assigned to this shard", len(files), len(target_files))

    out_dir = Path("batches") / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    failed = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for repo_path in target_files:
            try:
                raw = download_via_cdn(repo_path)
                pair = project_to_pair(raw, repo_path)

                # Dedup by content md5
                md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
                if is_duplicate(md5):
                    skipped_dup += 1
                    continue

                record = {
                    "prompt": pair["prompt"],
                    "response": pair["response"],
                }
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

                if (written % 100) == 0:
                    log.info("Written %d records", written)
            except Exception as exc:
                failed += 1
                log.warning("Failed %s: %s", repo_path, exc, exc_info=False)

    log.info("Finished: written=%d skipped_dup=%d failed=%d out=%s", written, skipped_dup, failed, out_path)

    # Push to HF dataset repo (single commit per shard run)
    if written > 0:
        try:
            from huggingface_hub import upload_file
            upload_file(
                path_or_fileobj=str(out_path),
                path_in
