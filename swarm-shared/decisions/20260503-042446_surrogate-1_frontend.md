# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Single API call (post-rate-limit window) to `list_repo_tree(path, recursive=False)` for one date folder → save `manifest.json`
   - Worker uses **CDN-only fetches** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header → bypasses `/api/` rate limits entirely
   - Per-file schema projection to `{prompt, response}` only at parse time → prevents `pyarrow.CastError` on heterogeneous files
   - Deterministic shard assignment via `hash(slug) % 16 == SHARD_ID`
   - Central md5 dedup via existing `lib/dedup.py`
   - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`

2. **`lib/dedup.py`** (unchanged) — keep as central SQLite store

3. **`.github/workflows/ingest.yml`** — update to run Python worker instead of shell

4. **`requirements.txt`** — add `requests` if not present

---

### Code Snippets

#### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 python bin/dataset-enrich.py 2024-01-15

Environment:
  HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
  SHARD_ID          - 0..15 (deterministic shard assignment)
  MANIFEST_PATH     - optional path to manifest.json (skip list_repo_tree)
"""
import json
import os
import sys
import hashlib
import datetime
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.csv as pcsv
import pyarrow.json as pj
import requests
from huggingface_hub import HfApi, hf_hub_download

REPO = "axentx/surrogate-1-training-pairs"
API = HfApi()
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
assert 0 <= SHARD_ID <= 15, "SHARD_ID must be 0..15"

# Dedup store (central SQLite, shared across workers via HF Space)
DEDUP_DB = Path(__file__).parent.parent / "lib" / "dedup.db"

def _init_db() -> sqlite3.Connection:
    DEDUP_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DEDUP_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS seen_md5 (md5 TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def _is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5=?", (md5,))
    return cur.fetchone() is not None

def _mark_seen(conn: sqlite3.Connection, md5: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen_md5 (md5) VALUES (?)", (md5,))

def list_date_folder(date_str: str) -> list[str]:
    """Single API call: list files in date folder (non-recursive)."""
    items = API.list_repo_tree(REPO, path=date_str, recursive=False)
    # Keep only files we expect (parquet/jsonl/csv)
    return [it.rfilename for it in items if it.type == "file"]

def build_manifest(date_str: str, files: list[str]) -> dict:
    manifest = {
        "date": date_str,
        "repo": REPO,
        "files": files,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    out = Path("manifest.json")
    out.write_text(json.dumps(manifest, indent=2))
    return manifest

def load_manifest() -> dict:
    mp = Path("manifest.json")
    if mp.exists():
        return json.loads(mp.read_text())
    raise FileNotFoundError("manifest.json not found")

def shard_for_file(filename: str) -> int:
    """Deterministic shard assignment: hash(slug) % 16."""
    slug = Path(filename).stem
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return h % 16

def cdn_url(path: str) -> str:
    """CDN bypass URL (no auth, no API rate limit)."""
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

def project_to_pair(record: dict) -> dict | None:
    """
    Project heterogeneous schema to {prompt, response}.
    Returns None if unusable.
    """
    # Common field names seen across datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "completion", "output", "answer", "result"}

    prompt = None
    response = None

    rk = set(record.keys())
    for pk in prompt_keys:
        if pk in rk and record[pk] not in (None, ""):
            prompt = str(record[pk]).strip()
            break
    for rk_ in response_keys:
        if rk_ in rk and record[rk_] not in (None, ""):
            response = str(record[rk_]).strip()
            break

    if not prompt or not response:
        return None
    return {"prompt": prompt, "response": response}

def compute_md5(pair: dict) -> str:
    payload = f"{pair['prompt']}\n{pair['response']}".encode()
    return hashlib.md5(payload).hexdigest()

def process_file_cdn(path: str, conn: sqlite3.Connection) -> list[dict]:
    """Download via CDN, parse per-format, project schema, dedup."""
    url = cdn_url(path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.content

    pairs = []
    suffix = Path(path).suffix.lower()

    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(data))
            # Convert to list of dicts for flexible schema handling
            batch = table.to_batches()[0]
            cols = batch.schema.names
            for i in range(batch.num_rows):
                rec = {c: batch.column(c)[i].as_py() for c in cols}
                pair = project_to_pair(rec)
                if pair:
                    md5 = compute_md5(pair)
                    if not _is_duplicate(conn, md5):
                        _mark_seen(conn, md5)
                        pairs.append(pair)

        elif suffix == ".jsonl":
            for line in data.splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                pair = project_to_pair(rec)
                if pair:
                    md5 = compute_md5(pair)
                    if not _is_duplicate(conn, md5):
                        _mark_seen(conn, md5)
                        pairs.append(pair)

        elif suffix in (".json", ".csv"):
            # Arrow-based CSV/JSON for consistency
            if suffix == ".csv":
                table = pcsv.read_csv(pa.BufferReader(data))
            else:
                table = pj.read_json(pa.BufferReader(data))
            batch = table.to_batches()[0]
            cols = batch.schema.names
            for i in range(batch.num_rows):
                rec = {c: batch.column(c)[i].as_py() for c in cols}
                pair = project_to_pair(rec)
                if pair:
                    md5 = compute_md5(pair)
                    if not _is_duplicate(conn, md5):
                        _mark_seen(conn, md5)
                        pairs.append(pair)

        else:
            # Fallback: try hf_hub_download + datasets (slower, may hit API)
            local_path = hf_hub_download(REPO, path=path)
            import datasets
            ds = datasets.load_dataset("parquet", data_files=local_path, split="train")
            for rec in ds:
                pair = project_to_pair(rec)
               
