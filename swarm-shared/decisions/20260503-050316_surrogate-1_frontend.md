# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-first, CDN-only Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Changes

1. **Add `bin/worker.py`** — single, deterministic shard worker that:
   - Accepts `SHARD_ID` (0–15) and `TOTAL_SHARDS` (16) via env.
   - **Reads `manifest.json`** (pre-computed file list) instead of recursive API calls; falls back to one-time `list_repo_tree` only if manifest missing.
   - Filters by `shard_id` via deterministic hash of filename.
   - Downloads only assigned files via **HF CDN** (`resolve/main/…`) — zero API/auth calls during bulk download.
   - Projects each file to `{prompt, response}` at parse time (avoids `pyarrow.CastError` from mixed schemas).
   - Dedups via content hash and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

2. **Update `bin/dataset-enrich.sh`** — thin wrapper that:
   - Exports `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`.
   - Invokes `python3 bin/worker.py` with matrix `SHARD_ID`.

3. **Update `.github/workflows/ingest.yml`** — ensure:
   - `SHELL: /bin/bash` in defaults.
   - Matrix strategy uses `shard_id: [0..15]`.
   - Each job runs the same wrapper script.

4. **Add `requirements.txt`** entries if missing: `requests`, `tqdm`, `huggingface-hub`.

---

### Code Snippets

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
Manifest-first, CDN-only shard worker for surrogate-1 ingestion.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py
"""
import os
import sys
import json
import hashlib
import datetime
from pathlib import Path

import requests
from huggingface_hub import list_repo_tree

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", 0))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", 16))
TODAY = datetime.datetime.utcnow().strftime("%Y-%m-%d")
OUT_DIR = Path("batches/public-merged") / TODAY
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Central dedup store (shared across runners via mounted volume or HF Space SQLite)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore
dedup = DedupStore()

session = requests.Session()
if HF_TOKEN:
    session.headers.update({"Authorization": f"Bearer {HF_TOKEN}"})

def list_today_files():
    """List today's files from manifest.json; fallback to one-time HF API call."""
    manifest_path = Path("manifest.json")
    if manifest_path.exists():
        files = json.loads(manifest_path.read_text())
        print(f"[shard {SHARD_ID}] using cached manifest with {len(files)} files")
        return files

    print(f"[shard {SHARD_ID}] manifest missing; listing via HF API (single call)...")
    items = list_repo_tree(
        repo_id=HF_REPO,
        path=TODAY,
        repo_type="dataset",
        token=HF_TOKEN,
    )
    files = [it.rfilename for it in items if it.type == "file"]
    manifest_path.write_text(json.dumps(files, indent=2))
    print(f"[shard {SHARD_ID}] manifest saved ({len(files)} files)")
    return files

def belongs_to_shard(key: str) -> bool:
    """Deterministic shard assignment by hash."""
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return (h % TOTAL_SHARDS) == SHARD_ID

def cdn_download_url(path: str) -> str:
    """CDN URL that bypasses HF API auth/rate limits."""
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"

def parse_file_to_pair(content_bytes: bytes, filename: str):
    """
    Project heterogeneous file to {prompt,response} at parse time.
    Supports JSON/JSONL minimal shapes; extend per your schema variants.
    """
    text = content_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return

    # JSONL: one object per line
    if "\n" in text and text.startswith("{"):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt and response:
                yield {"prompt": str(prompt), "response": str(response), "source_file": filename}
        return

    # Single JSON object
    try:
        obj = json.loads(text)
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
        response = obj.get("response") or obj.get("output") or obj.get("answer")
        if prompt and response:
            yield {"prompt": str(prompt), "response": str(response), "source_file": filename}
        return
    except json.JSONDecodeError:
        pass

    # Fallback: treat as plain text Q/A heuristics if needed
    # (customize to your corpus)
    yield None

def run_shard():
    print(f"[shard {SHARD_ID}] loading manifest...")
    files = list_today_files()

    shard_files = [f for f in files if belongs_to_shard(f)]
    print(f"[shard {SHARD_ID}] processing {len(shard_files)} files")

    written = 0
    skipped_dup = 0
    out_f = OUT_FILE.open("w", buffering=1)

    for rel_path in shard_files:
        url = cdn_download_url(rel_path)
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"[shard {SHARD_ID}] download failed {rel_path}: {e}")
            continue

        content = resp.content
        # Compute md5 for dedup across sources
        md5 = hashlib.md5(content).hexdigest()
        if dedup.exists(md5):
            skipped_dup += 1
            continue

        pairs = list(parse_file_to_pair(content, rel_path))
        if not pairs:
            continue

        for pair in pairs:
            if not pair:
                continue
            line = json.dumps(pair, ensure_ascii=False)
            out_f.write(line + "\n")
            dedup.add(md5)
            written += 1

    out_f.close()
    print(f"[shard {SHARD_ID}] done. written={written}, skipped_dup={skipped_dup}, out={OUT_FILE}")

if __name__ == "__main__":
    run_shard()
```

#### `lib/dedup.py` (minimal, extend as needed)
```python
import sqlite3
from pathlib import Path

class DedupStore:
    def __init__(self, db_path=None):
        if db_path is None:
            # Default to local file; in HF Space this can be a mounted persistent path
            db_path = Path("dedup_store.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")

    def exists(self, md5: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM seen WHERE md5 = ?", (md5,))
            return cur.fetchone() is not None

    def add
