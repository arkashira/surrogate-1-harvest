# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree` call per date → deterministic manifest
- Workers use **manifest + CDN-only fetches** (no HF API during stream) to bypass 429 rate limits
- Deterministic shard assignment by `hash(slug) % SHARD_TOTAL`
- Projects heterogeneous source files to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Dedup via central `lib/dedup.py` md5 store
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Keeps wrapper executable + Bash shebang for cron compatibility

---

### Steps (1h 30m)

1. **Create `bin/dataset-enrich.py`** (45m)
   - Env parsing + logging
   - `list_repo_tree(recursive=False)` per date folder (single API call)
   - Build manifest JSON: `{ "date": "...", "files": [...] }`
   - Deterministic shard filter
   - Stream each file via CDN URL (no auth header)
   - Schema-agnostic extractor → `{prompt, response, md5}`
   - Dedup check via `lib/dedup.py`
   - Append to shard output file

2. **Update `lib/dedup.py`** (15m)
   - Ensure thread-safe SQLite access (single writer per runner is fine)
   - Add `seen(md5) -> bool` and `add(md5)` helpers

3. **Update GitHub Actions matrix** (15m)
   - Pass `DATE` (YYYY-MM-DD) and `FILE_LIST` artifact from a prior “list” job (optional) or embed list in runner
   - Keep 16-shard matrix

4. **Remove `bin/dataset-enrich.sh`** (5m)

5. **Test locally** (10m)
   - Dry-run with `SHARD_TOTAL=2`, `SHARD_ID=0`, small date folder

---

### Code Snippets

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Env:
  SHARD_ID (int, required)
  SHARD_TOTAL (int, default=16)
  DATE (YYYY-MM-DD, required)
  HF_TOKEN (str, required for upload)
  DATASET_REPO (default=axentx/surrogate-1-training-pairs)
  DEDUP_DB (path, default=lib/dedup.db)
"""

import os
import sys
import json
import hashlib
import logging
import datetime
import sqlite3
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests
from huggingface_hub import HfApi

# Local
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-enrich")

# Config
SHARD_ID = int(os.environ.get("SHARD_ID", 0))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", 16))
DATE = os.environ.get("DATE")
HF_TOKEN = os.environ.get("HF_TOKEN")
DATASET_REPO = os.environ.get("DATASET_REPO", "axentx/surrogate-1-training-pairs")
DEDUP_DB = os.environ.get("DEDUP_DB", str(REPO_ROOT / "lib" / "dedup.db"))

if not DATE:
    log.error("DATE (YYYY-MM-DD) is required")
    sys.exit(1)
if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

api = HfApi(token=HF_TOKEN)

def slug_hash_bucket(slug: str, n: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n

def list_date_files(date: str) -> List[str]:
    """Single API call: list top-level files in date folder (non-recursive)."""
    folder = f"batches/public-merged/{date}"
    try:
        items = api.list_repo_tree(repo_id=DATASET_REPO, path=folder, recursive=False)
    except Exception as e:
        log.warning(f"list_repo_tree failed for {folder}: {e}")
        items = []
    files = []
    for it in items:
        if isinstance(it, dict):
            p = it.get("path", "")
        else:
            p = getattr(it, "path", str(it))
        if p and not p.endswith("/"):
            files.append(p)
    log.info(f"Found {len(files)} files in {folder}")
    return files

def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main/{path}"

def extract_pair(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Schema-agnostic extractor.
    Returns {prompt, response} or None if not extractable.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def md5_of_text(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

class DedupStore:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")

    def seen(self, md5: str) -> bool:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute("SELECT 1 FROM seen WHERE md5 = ?", (md5,))
            return cur.fetchone() is not None

    def add(self, md5: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT OR IGNORE INTO seen (md5) VALUES (?)", (md5,))

dedup = DedupStore(DEDUP_DB)

def process_file(path: str) -> List[Dict[str, Any]]:
    """Download via CDN, parse, extract pairs, dedup."""
    url = cdn_url(path)
    out = []
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return out

    if path.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq
            import io
            table = pq.read_table(io.BytesIO(resp.content))
            rows = table.to_pylist()
        except Exception as e:
            log.warning(f"Parquet decode failed for {path}: {e}")
            return out
    elif path.endswith(".jsonl"):
        rows = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    else:
        log.debug(f"Skipping unsupported file: {path}")
        return out

    for row in rows:
        pair = extract_pair(row)
        if not pair:
            continue
        combined = pair["prompt"] + "\n" + pair["response"]
        md5 = md5_of_text(combined)
        if dedup.seen(md5):
            continue
        dedup.add(md5)
        out.append({"prompt": pair["prompt"], "response": pair["response"], "md5": md5})
    return out

def main() -> None:
    log.info(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for date {DATE}")
    files = list_date_files(DATE)
    if not files:
        log.warning("No files found; exiting")

