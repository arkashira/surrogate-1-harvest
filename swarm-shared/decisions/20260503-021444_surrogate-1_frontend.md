# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `DATASET_REPO` (default: `axentx/surrogate-1-training-pairs`)
- Single API call from runner to list one date folder via `list_repo_tree(..., recursive=False)` → save to `manifest.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes output to `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Commits via HF API (rate-limit aware; 429 → wait 360s)
- Exits 0 on success, non-zero on fatal error

### Steps (est. 90 min)
1. Create `bin/dataset-enrich.py` (60 min)
2. Update `.github/workflows/ingest.yml` to use Python script and pass env vars (15 min)
3. Add `requirements-dev.txt` (if needed) and ensure `lib/dedup.py` interface compatibility (15 min)

---

## Code Snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GitHub Actions):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          - integer 0..SHARD_TOTAL-1
  SHARD_TOTAL       - total shards (default 16)
  DATE              - date folder to ingest (YYYY-MM-DD)
  HF_TOKEN          - HuggingFace write token
  DATASET_REPO      - dataset repo (default: axentx/surrogate-1-training-pairs)
  DEDUP_DB_PATH     - path to central dedup sqlite (default: lib/dedup.db)
"""

import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("surrogate-ingest")

# ---------- config ----------
SHARD_ID = int(os.getenv("SHARD_ID", 0))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
DEDUP_DB_PATH = os.getenv("DEDUP_DB_PATH", "lib/dedup.db")

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_BASE = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main"

# ---------- dedup ----------
def ensure_dedup_db() -> None:
    db_path = Path(DEDUP_DB_PATH)
    if db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # minimal schema compatible with lib/dedup.py expectations
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY, ts REAL)")
    conn.commit()
    conn.close()
    log.info("Initialized dedup db at %s", db_path)

def is_duplicate(md5_hex: str) -> bool:
    import sqlite3
    conn = sqlite3.connect(str(DEDUP_DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen WHERE md5=?", (md5_hex,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def mark_seen(md5_hex: str) -> None:
    import sqlite3
    conn = sqlite3.connect(str(DEDUP_DB_PATH))
    conn.execute("INSERT OR IGNORE INTO seen (md5, ts) VALUES (?, ?)", (md5_hex, time.time()))
    conn.commit()
    conn.close()

# ---------- manifest ----------
def build_manifest(date_folder: str) -> List[str]:
    """List files in date folder (non-recursive) and save manifest.json."""
    log.info("Listing repo tree for %s in %s", DATASET_REPO, date_folder)
    try:
        tree = list_repo_tree(repo_id=DATASET_REPO, path=date_folder, recursive=False)
    except Exception as exc:
        log.error("Failed to list repo tree: %s", exc)
        raise

    filenames = [item.rfilename for item in tree if item.rfilename]
    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps(filenames, indent=2))
    log.info("Saved manifest with %d files to %s", len(filenames), manifest_path)
    return filenames

# ---------- shard assignment ----------
def shard_for_filename(filename: str) -> int:
    """Deterministic shard by hash(slug) % SHARD_TOTAL."""
    slug = Path(filename).stem
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % SHARD_TOTAL

# ---------- parse helpers ----------
def parse_file_to_pairs(local_path: Path) -> List[Dict[str, str]]:
    """
    Project file to {prompt, response} pairs at parse time.
    Supports common surrogate formats:
      - JSONL with 'prompt'/'response' or 'input'/'output'
      - Parquet (via pyarrow) projected to string columns
    """
    import pyarrow.parquet as pq
    import pyarrow as pa

    pairs = []
    suffix = local_path.suffix.lower()

    try:
        if suffix == ".jsonl":
            for line in local_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})

        elif suffix == ".parquet":
            # Project only string-like columns to avoid mixed-schema CastError
            pf = pq.read_table(local_path, columns=[])
            schema = pf.schema
            # find candidate columns
            prompt_col = None
            response_col = None
            for i, field in enumerate(schema):
                name = field.name.lower()
                if "prompt" in name or "input" in name:
                    prompt_col = field.name
                if "response" in name or "output" in name:
                    response_col = field.name

            if prompt_col and response_col:
                table = pq.read_table(local_path, columns=[prompt_col, response_col])
            else:
                # fallback: read first two string columns
                str_cols = [f.name for f in schema if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)]
                if len(str_cols) >= 2:
                    table = pq.read_table(local_path, columns=str_cols[:2])
                else:
                    log.warning("No suitable columns in %s", local_path)
                    return []

            df = table.to_pandas()
            colnames = df.columns.tolist()
            prompt_col = colnames[0]
            response_col = colnames[1]
            for _, row in df.iterrows():
                p = str(row[prompt_col]) if pd_notna(row[prompt_col]) else ""
                r = str(row[response_col]) if pd_notna(row[response_col]) else
