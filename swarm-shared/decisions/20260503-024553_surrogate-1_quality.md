# surrogate-1 / quality

## Final Implementation Plan (≤2 h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses **one** `list_repo_tree` call (per date folder) to enumerate parquet files and saves `manifest-{DATE}.json`
- Deterministic shard assignment via `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads assigned files via **HF CDN bypass** (`resolve/main/...`) with **no Authorization header during data fetch** (token used only for listing and final push)
- Projects each file to `{prompt, response}` only at parse time to avoid pyarrow CastError on mixed schemas
- Deduplicates via central `lib/dedup.py` md5 store
- Outputs: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Adds retry/backoff for CDN downloads and atomic write on success

---

### 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Behavior:
- list_repo_tree(date_folder) once -> manifest-{DATE}.json
- deterministic shard assignment by hash(slug) % SHARD_TOTAL
- CDN-only downloads (no auth header) to bypass HF API rate limits
- project to {prompt,response} at parse time (mixed-schema safe)
- dedup via lib/dedup.py (central md5 store)
- output: batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
- retries + exponential backoff on CDN failures; atomic write on success
"""

import os
import sys
import json
import hashlib
import datetime
import time
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional

import pyarrow.parquet as pq
import pyarrow as pa
import requests
from huggingface_hub import HfApi

# --
# Configuration
# --
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
OUTPUT_DIR = Path("batches/public-merged") / DATE
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"
MANIFEST_FILE = Path("batches/public-merged") / f"manifest-{DATE}.json"

# Retry/backoff
MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 30.0

# --
# Dedup store (reuse existing)
# --
sys.path.insert(0, str(Path(__file__).parent / "lib"))
try:
    from dedup import DedupStore
except Exception:
    # Fallback minimal dedup if lib/dedup.py unavailable
    class DedupStore:
        def __init__(self, db_path=":memory:"):
            self.seen = set()
        def exists(self, md5: str) -> bool:
            return md5 in self.seen
        def add(self, md5: str) -> None:
            self.seen.add(md5)

dedup = DedupStore()

# --
# HF API (used only for listing)
# --
api = HfApi(token=HF_TOKEN)

def list_date_files(date_folder: str) -> List[str]:
    """Single list_repo_tree call for a date folder (non-recursive)."""
    try:
        tree = api.list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"[ERROR] HF list_repo_tree failed: {e}", file=sys.stderr)
        return []

    files = [item.path for item in tree if item.type == "file" and item.path.endswith(".parquet")]
    return sorted(files)

def deterministic_shard(filepath: str) -> int:
    """Shard assignment by hash(slug) % SHARD_TOTAL."""
    slug = Path(filepath).stem
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % SHARD_TOTAL

def cdn_url(filepath: str) -> str:
    """CDN URL (no auth)."""
    return f"{BASE_CDN}/{filepath}"

def safe_md5(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def project_to_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project arbitrary schema to {prompt, response}.
    Common field names seen in wild:
      prompt/response, instruction/input/output, question/answer,
      user/assistant, text/target, content/completion
    """
    if not isinstance(obj, dict):
        return None

    # Normalize keys to lowercase for matching
    low = {k.lower(): v for k, v in obj.items() if isinstance(v, (str, type(None)))}

    prompt = None
    response = None

    # Priority pairs
    if "prompt" in low and "response" in low:
        prompt, response = low["prompt"], low["response"]
    elif "instruction" in low and ("output" in low or "response" in low):
        prompt = low["instruction"]
        response = low.get("output") or low.get("response")
    elif "question" in low and "answer" in low:
        prompt, response = low["question"], low["answer"]
    elif "user" in low and "assistant" in low:
        prompt, response = low["user"], low["assistant"]
    elif "content" in low and "completion" in low:
        prompt, response = low["content"], low["completion"]
    elif "text" in low and "target" in low:
        prompt, response = low["text"], low["target"]
    elif "text" in low:
        # Single text: treat as prompt, empty response
        prompt = low["text"]
        response = ""

    if prompt is None or response is None:
        return None

    # Ensure strings
    prompt = "" if prompt is None else str(prompt).strip()
    response = "" if response is None else str(response).strip()

    if not prompt and not response:
        return None

    return {"prompt": prompt, "response": response}

def stream_parquet_cdn(filepath: str, batch_size: int = 1000):
    """Stream parquet via CDN URL and yield rows as dicts."""
    url = cdn_url(filepath)
    # Use pyarrow's native HTTP filesystem (no auth)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            dataset = pq.ParquetDataset(
                url,
                filesystem=pa.fs.HttpFileSystem(),
                batch_size=batch_size,
            )
            for batch in dataset.to_batches():
                tbl = pa.Table.from_batches([batch])
                for i in range(tbl.num_rows):
                    yield tbl.slice(i, 1).to_pylist()[0]
            return
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
            print(f"[WARN] CDN streaming failed for {filepath} (attempt {attempt}): {e}; retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)

def process_file(filepath: str, shard_id: int) -> List[Dict[str, Any]]:
    """Process one parquet file and return pairs belonging to this shard."""
    if deterministic_shard(filepath) != shard_id:
        return []

    out = []
