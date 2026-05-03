# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Single API call from runner to list one date folder via `list_repo_tree(recursive=False)` → deterministic shard assignment by hash(slug)
- Downloads only assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (avoids 429 /api/ limits)
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with slug-derived attribution in filename only (no extra columns)
- Exits non-zero on unrecoverable errors; logs summary for Actions

---

### Code

`bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage (local/test):
  DATE=2026-05-03 SHARD_ID=0 SHARD_TOTAL=16 \
  HF_TOKEN=hf_xxx REPO_ID=axentx/surrogate-1-training-pairs \
  python bin/dataset-enrich.py

GitHub Actions matrix:
  strategy:
    matrix:
      shard: [0,1,2,...,15]
  env:
    SHARD_ID: ${{ matrix.shard }}
    SHARD_TOTAL: 16
    DATE: ${{ github.run_number }}-$(date +%Y-%m-%d)
"""

import os
import sys
import json
import hashlib
import logging
import datetime as dt
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests
from huggingface_hub import HfApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dataset-enrich")

# ---------- config ----------
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", dt.datetime.utcnow().strftime("%Y-%m-%d"))
BASE_DIR = Path(__file__).parent.parent

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# ---------- dedup ----------
sys.path.insert(0, str(BASE_DIR / "lib"))
try:
    from dedup import DedupStore
except Exception as e:
    log.warning("dedup import failed: %s; using local sqlite fallback", e)
    import sqlite3
    DEDUP_DB = BASE_DIR / "dedup.db"
    DEDUP_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DEDUP_DB), check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
    conn.commit()

    class DedupStore:
        def __init__(self):
            self.conn = sqlite3.connect(str(DEDUP_DB), check_same_thread=False)

        def exists(self, md5: str) -> bool:
            cur = self.conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
            return cur.fetchone() is not None

        def add(self, md5: str) -> None:
            try:
                self.conn.execute("INSERT INTO seen(md5) VALUES (?)", (md5,))
                self.conn.commit()
            except sqlite3.IntegrityError:
                pass

# ---------- helpers ----------
def deterministic_shard(slug: str, total: int) -> int:
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % total

def list_date_files(date: str) -> List[str]:
    """
    Single API call: list top-level files in date folder.
    Avoids recursive list_repo_files on big repos.
    """
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=date, recursive=False)
        files = [item.rfilename for item in tree if getattr(item, "type", None) == "file"]
        log.info("listed %d files in %s/%s", len(files), REPO_ID, date)
        return files
    except Exception as e:
        log.error("list_repo_tree failed: %s", e)
        raise

def cdn_download(repo_id: str, path: str) -> bytes:
    """
    CDN bypass: no Authorization header.
    Public URL: https://huggingface.co/datasets/{repo_id}/resolve/main/{path}
    """
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def normalize_record(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Common patterns:
      - {prompt, response}
      - {input, output}
      - {question, answer}
      - {instruction, completion}
      - {text} -> split by separator if possible
    """
    if not isinstance(raw, dict):
        return None

    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or raw.get("instruction")
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion")

    if prompt is not None and response is not None:
        return {"prompt": str(prompt).strip(), "response": str(response).strip()}

    text = raw.get("text")
    if isinstance(text, str):
        parts = text.split("\n\n")
        if len(parts) >= 2:
            return {"prompt": parts[0].strip(), "response": parts[1].strip()}
    return None

def parse_file(content: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Parse parquet/jsonl/json and yield normalized records.
    """
    import io

    records: List[Dict[str, str]] = []
    name = filename.lower()

    try:
        if name.endswith(".parquet"):
            import pyarrow.parquet as pq
            import pyarrow as pa

            table = pq.read_table(io.BytesIO(content))
            cols = set(table.column_names)
            prompt_col = next((c for c in ["prompt", "input", "question", "instruction"] if c in cols), None)
            response_col = next((c for c in ["response", "output", "answer", "completion"] if c in cols), None)

            if prompt_col and response_col:
                df = table.select([prompt_col, response_col]).to_pandas()
                for _, row in df.iterrows():
                    records.append({"prompt": str(row[prompt_col]).strip(), "response": str(row[response_col]).strip()})
            else:
                for row in table.to_pylist():
                    rec = normalize_record(row)
                    if rec:
                        records.append(rec)

        elif name.endswith(".jsonl"):
            for line in content.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                rec = normalize_record(raw)
                if rec:
                    records.append(rec)

        elif name.endswith(".json"):
            raw = json.loads(content.decode("utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    rec = normalize_record(item)
                    if rec:
                        records.append(rec)
            else:
                rec = normalize_record(raw)
                if rec:
                    records.append(rec)

        else:
            log.warning("unsupported file type: %s", filename)

    except Exception as e:
        log.error("parse
