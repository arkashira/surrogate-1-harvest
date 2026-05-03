# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — single-file worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` from the matrix
   - Calls `list_repo_tree(recursive=False)` once per date folder (post-rate-limit window) and writes `manifest.json`
   - Downloads only assigned shard’s files via **CDN bypass** (`resolve/main/...` with no auth)
   - Projects each file to `{prompt, response}` at parse time (avoids `load_dataset` mixed-schema CastError)
   - Deduplicates via `lib/dedup.py` (existing SQLite store)
   - Streams output to `shard-<N>-<HHMMSS>.jsonl` in `batches/public-merged/<date>/`
   - Uploads results via HF API (uses `HF_TOKEN` only for writes)

2. **Update `bin/dataset-enrich.sh`** — thin wrapper:
   - Sets `SHELL=/bin/bash`
   - Exports `PYTHONUNBUFFERED=1`
   - Invokes `python3 bin/worker.py "$@"`
   - `chmod +x` preserved

3. **Update `.github/workflows/ingest.yml`** — ensure:
   - Matrix `strategy.shard` passed as `SHARD_ID`
   - `HF_TOKEN` available for write (dedup + upload) but not used for training file reads
   - Uses `ubuntu-latest` with no special HF auth for CDN downloads

4. **Add `requirements.txt` entries** if missing:
   - `requests` (CDN downloads)
   - `tqdm` (optional progress)

---

## Code Snippets

### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass worker for surrogate-1 public-dataset ingestion.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py [--date 2026-04-29]
"""

import json
import os
import sys
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, list_repo_tree

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
API = HfApi()

# ── config --
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
DATE = os.getenv("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
MANIFEST_PATH = Path(f"manifest-{DATE}.json")
OUTPUT_DIR = Path("batches/public-merged") / DATE
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard-{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── dedup --
def get_dedup_conn() -> sqlite3.Connection:
    # Reuse existing central store if present; fallback to local
    db_path = Path("lib/dedup.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_md5 (md5 TEXT PRIMARY KEY, ts TEXT)"
    )
    conn.commit()
    return conn

def is_duplicate(md5: str, conn: sqlite3.Connection) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_md5 WHERE md5=?", (md5,))
    return cur.fetchone() is not None

def mark_seen(md5: str, conn: sqlite3.Connection) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT OR IGNORE INTO seen_md5 (md5, ts) VALUES (?, ?)", (md5, ts))

# ── manifest --
def build_manifest() -> List[str]:
    """List top-level date folder via API once and cache manifest.json."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())

    tree = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE,
        recursive=False,
        token=os.getenv("HF_TOKEN"),
    )
    files = [item.rfilename for item in tree if item.type == "file"]
    MANIFEST_PATH.write_text(json.dumps(files, indent=2))
    return files

# ── cdn download --
def cdn_download(repo_path: str) -> bytes:
    """Download via CDN without Authorization header (bypasses /api/ rate limits)."""
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{repo_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

# ── project to {prompt,response} --
def project_to_pair(data: bytes, ext: str) -> Dict[str, str]:
    """Project heterogeneous file to {prompt, response} only."""
    if ext == ".parquet":
        tbl = pq.read_table(pa.BufferReader(data))
        # Best-effort column mapping
        prompt_col = next((c for c in tbl.column_names if "prompt" in c.lower()), None)
        response_col = next((c for c in tbl.column_names if "response" in c.lower()), None)
        if prompt_col is None or response_col is None:
            # fallback: first two string cols
            str_cols = [c for c in tbl.column_names if pa.types.is_string(tbl.schema.field(c).type)]
            if len(str_cols) >= 2:
                prompt_col, response_col = str_cols[0], str_cols[1]
            else:
                raise ValueError("Cannot find prompt/response columns")
        # Take first row only for this worker model (streaming semantics)
        prompt = str(tbl.column(prompt_col)[0].as_py())
        response = str(tbl.column(response_col)[0].as_py())
        return {"prompt": prompt, "response": response}

    elif ext == ".jsonl":
        lines = data.decode("utf-8").strip().splitlines()
        if not lines:
            raise ValueError("Empty jsonl")
        obj = json.loads(lines[0])
        prompt = obj.get("prompt") or obj.get("input") or ""
        response = obj.get("response") or obj.get("output") or ""
        return {"prompt": str(prompt), "response": str(response)}

    else:
        raise ValueError(f"Unsupported extension: {ext}")

# ── shard assignment --
def assign_shard(files: List[str]) -> List[str]:
    """Deterministic shard assignment by slug hash."""
    assigned = []
    for f in files:
        slug = Path(f).stem
        h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
        if h % TOTAL_SHARDS == SHARD_ID:
            assigned.append(f)
    return assigned

# ── main --
def main() -> None:
    print(f"Worker shard={SHARD_ID}/{TOTAL_SHARDS} date={DATE}")
    files = build_manifest()
    assigned = assign_shard(files)
    print(f"Assigned {len(assigned)} files")

    conn = get_dedup_conn()
    written = 0

    with OUTPUT_FILE.open("w", encoding="utf-8") as fout:
        for repo_path in assigned:
            try:
                data = cdn_download(repo_path)
                pair = project_to_pair(data, Path(repo_path).suffix.lower())
                # dedup by content hash
                md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
                if is_duplicate(md5, conn):
                    continue
                mark_seen(md5, conn)
                fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1
            except Exception as exc:
                print(f"Skip {repo_path}: {exc}", file=sys.stderr)

    conn.commit()
    conn.close()

    # Upload via huggingface_h
