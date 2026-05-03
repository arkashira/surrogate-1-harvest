# surrogate-1 / backend

**Final, consolidated implementation plan**  
*(Strongest parts from both candidates; contradictions resolved for correctness + concrete actionability)*

---

## 1. Goal (≤2 hours)
Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today UTC `YYYY-MM-DD`)
- Uses **one `list_repo_tree` call per date folder** → local JSON manifest → Lightning training does CDN-only fetches with **zero API calls** during data load
- Projects heterogeneous HF dataset files to `{prompt, response}` only at parse time (avoids pyarrow `CastError`)
- Deterministic shard assignment via `hash(slug) % SHARD_TOTAL`
- Central md5 dedup via existing `lib/dedup.py`
- Outputs to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Respects HF rate limits: on 429, wait 360 s and retry; uses CDN URLs (and `hf_hub_download` fallback) to bypass `/api/` auth limits
- Runs as executable Python script (no Bash wrapper issues)

---

## 2. Steps (≤2 hours)

1. **Create `bin/dataset-enrich.py`** (main worker)  
2. **Update `bin/dataset-enrich.sh`** → thin wrapper that calls `python bin/dataset-enrich.py` (preserve backward compat)  
3. **Ensure `lib/dedup.py` is importable** (add `__init__.py` in `lib/` if missing)  
4. **Update `.github/workflows/ingest.yml`** to use matrix `SHARD_ID`/`SHARD_TOTAL` and run `python bin/dataset-enrich.py`  
5. **Test locally** with `DRY_RUN=1`

---

## 3. `bin/dataset-enrich.py` (final, production-ready)

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-04-29 python bin/dataset-enrich.py
  DRY_RUN=1 SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
"""

import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from huggingface_hub import HfApi, list_repo_tree

# Local dedup
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from lib.dedup import DedupStore  # noqa: E402
except ImportError as e:
    print(f"ERROR: Cannot import lib.dedup: {e}", file=sys.stderr)
    sys.exit(1)

# ---- Config ----
REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

OUT_DIR = Path("batches/public-merged") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

API = HfApi(token=HF_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
CDN_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

DEDUP_DB = os.getenv("DEDUP_DB", "dedup.db")
dedup = DedupStore(DEDUP_DB)

MAX_RETRIES = 5
RETRY_WAIT = 360  # seconds on 429

# ---- Helpers ----
def get_date_folder_manifest(date_folder: str) -> List[str]:
    """List files in a date folder (non-recursive) via HF API."""
    for attempt in range(MAX_RETRIES):
        try:
            tree = list_repo_tree(
                repo_id=REPO_ID,
                path=date_folder,
                recursive=False,
                token=HF_TOKEN,
            )
            files = [f.rfilename for f in tree if f.type == "file"]
            return files
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate limit" in msg.lower():
                if attempt < MAX_RETRIES - 1:
                    print(f"[{SHARD_ID}] Rate limited (429). Waiting {RETRY_WAIT}s...", file=sys.stderr)
                    time.sleep(RETRY_WAIT)
                    continue
            print(f"[{SHARD_ID}] Failed to list repo tree: {e}", file=sys.stderr)
            raise
    raise RuntimeError(f"[{SHARD_ID}] Max retries exceeded listing {date_folder}")

def deterministic_shard(key: str, total: int) -> int:
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    return h % total

def download_cdn(url: str, dest: Path) -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate limit" in msg.lower():
                if attempt < MAX_RETRIES - 1:
                    print(f"[{SHARD_ID}] CDN 429. Waiting {RETRY_WAIT}s...", file=sys.stderr)
                    time.sleep(RETRY_WAIT)
                    continue
            print(f"[{SHARD_ID}] Download failed {url}: {e}", file=sys.stderr)
            return False
    return False

def parse_file_to_pair(local_path: Path) -> Optional[Dict[str, str]]:
    """
    Parse heterogeneous HF dataset files to {prompt, response}.
    Supports JSON/JSONL and Parquet (projected to prompt/response).
    """
    suffix = local_path.suffix.lower()

    try:
        if suffix == ".parquet":
            import pyarrow.parquet as pq
            try:
                table = pq.read_table(local_path, columns=["prompt", "response"])
            except (ValueError, KeyError):
                try:
                    table = pq.read_table(local_path, columns=["instruction", "output"])
                    table = table.rename_columns(["prompt", "response"])
                except Exception:
                    # Fallback: read first two string columns
                    schema = pq.read_schema(local_path)
                    cols = [n for n in schema.names if schema.field(n).type in (str, "string")]
                    if len(cols) < 2:
                        return None
                    table = pq.read_table(local_path, columns=cols[:2])
                    table = table.rename_columns(["prompt", "response"])
            df = table.to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": str(row["prompt"]), "response": str(row["response"])}
            return

        # JSON/JSONL
        if suffix == ".jsonl":
            with open(local_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        elif suffix == ".json":
            with open(local_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                lines = [content] if not content.startswith("[") else json.loads(content)
        else:
            return

        for line in lines:
            if isinstance(line, str):
                line = json.loads(line)
            if not isinstance(line, dict):
                continue
            prompt = line.get("prompt") or line
