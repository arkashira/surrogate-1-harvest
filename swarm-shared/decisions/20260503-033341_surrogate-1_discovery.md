# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (optional)
- Single API call: `list_repo_tree(path=DATE, recursive=False)` → saves `manifest.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedup via central md5 store (`lib/dedup.py`)
- Outputs: `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Retries with exponential backoff; respects HF 429 (wait 360s)
- Reusable across cron/GitHub Actions matrix

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

# --
# Config
# --
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
BATCH_OUT_DIR = Path("batches/public-merged")
HF_TOKEN = os.getenv("HF_TOKEN", "")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MAX_RETRIES = 5
BACKOFF = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# --
# Dedup store (central md5)
# --
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

dedup = DedupStore()

# --
# Helpers
# --
def deterministic_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def api_get(path: str, use_auth: bool = False, **kwargs) -> requests.Response:
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if use_auth else {}
    url = f"https://huggingface.co/api/{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = 360
                log.warning("HF API 429 — waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            sleep = BACKOFF * (2 ** (attempt - 1))
            log.warning("Request failed (%s), retry %s/%s in %ss: %s", path, attempt, MAX_RETRIES, sleep, exc)
            time.sleep(sleep)
    raise RuntimeError(f"Failed after retries: {path}")

def list_date_folder(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    resp = api_get(f"datasets/{REPO_ID}/tree?path={date_folder}&recursive=false", use_auth=True)
    entries = resp.json()
    files = [e["path"] for e in entries if e.get("type") == "file"]
    log.info("Listed %s files in %s", len(files), date_folder)
    return files

def download_via_cdn(file_path: str, local_path: Path) -> Path:
    url = f"{BASE_CDN}/{file_path}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return local_path
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            sleep = BACKOFF * (2 ** (attempt - 1))
            log.warning("CDN download failed %s, retry %s/%s in %ss: %s", file_path, attempt, MAX_RETRIES, sleep, exc)
            time.sleep(sleep)
    raise RuntimeError(f"CDN download failed after retries: {file_path}")

def project_to_pair(raw_obj: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Heuristic: look for common field names; fallback to first/second text-like fields.
    """
    prompt = None
    response = None

    # Common field names
    prompt_candidates = ["prompt", "instruction", "input", "question", "user"]
    response_candidates = ["response", "completion", "output", "answer", "assistant"]

    for k in prompt_candidates:
        if k in raw_obj and isinstance(raw_obj[k], str) and raw_obj[k].strip():
            prompt = raw_obj[k].strip()
            break
    for k in response_candidates:
        if k in raw_obj and isinstance(raw_obj[k], str) and raw_obj[k].strip():
            response = raw_obj[k].strip()
            break

    # Fallback: pick first and second string fields
    if prompt is None or response is None:
        str_fields = [v.strip() for v in raw_obj.values() if isinstance(v, str) and v.strip()]
        if len(str_fields) >= 2:
            if prompt is None:
                prompt = str_fields[0]
            if response is None:
                response = str_fields[1]

    # If still missing, use JSON projection
    if prompt is None:
        prompt = json.dumps({k: v for k, v in raw_obj.items() if k not in response_candidates}, ensure_ascii=False)
    if response is None:
        response = json.dumps({k: v for k, v in raw_obj.items() if k in response_candidates}, ensure_ascii=False)

    return {"prompt": prompt, "response": response}

def parse_file(file_path: Path) -> List[Dict[str, str]]:
    """Parse parquet/jsonl/json and project to pairs."""
    import pyarrow.parquet as pq
    suffix = file_path.suffix.lower()
    pairs = []

    try:
        if suffix == ".parquet":
            table = pq.read_table(file_path)
            for i in range(table.num_rows):
                raw = {col: table[col][i].as_py() for col in table.column_names}
                pairs.append(project_to_pair(raw))
        elif suffix == ".jsonl":
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    pairs.append(project_to_pair(raw))
        elif suffix == ".json":
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for raw in data:
                        pairs.append(project_to_pair(raw))
                else:
                    pairs.append(project_to_pair(data))
        else:
            log.warning("Unsupported file %s, skipping", file_path)
   
