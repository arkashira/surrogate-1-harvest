# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed file manifest** (`batches/public-merged/<date>/manifest.json`) to avoid recursive HF API calls and rate limits.
- Downloads only its deterministic shard slice via **HF CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero Authorization header, bypasses `/api/` rate limits.
- Projects each file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` on mixed-schema repos).
- Deduplicates via central `lib/dedup.py` md5 store.
- Writes output as `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` and atomically uploads via HF Hub (one commit per shard per run).
- Reuses existing GitHub Actions matrix (`ingest.yml`) — no workflow changes required.

---

### Steps (estimated 90 min)

1. **Create `bin/dataset-enrich.py`** (60 min) — manifest loader, CDN downloader, schema projector, dedup, upload.
2. **Add lightweight manifest generator `bin/gen-manifest.py`** (10 min) — run once per date folder to list files (uses HF API once, then CDN-only).
3. **Update `requirements.txt`** (5 min) — ensure `requests`, `tqdm`, `pyarrow`, `datasets`, `huggingface_hub`.
4. **Remove/Deprecate `bin/dataset-enrich.sh`** (5 min) — keep as symlink or backup for now.
5. **Smoke test locally** (10 min) — run with `SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03`.

---

### Code Snippets

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 \
  HF_TOKEN=<write-token> \
  python bin/dataset-enrich.py [--date-folder YYYY-MM-DD]
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any

import requests
from tqdm import tqdm
from huggingface_hub import HfApi, hf_hub_download

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # expects DedupStore with .seen(md5) -> bool and .add(md5)

# ---- config ----
HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())

MANIFEST_PATH = f"batches/public-merged/{DATE_FOLDER}/manifest.json"
OUT_DIR = Path("output") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

DEDUP = DedupStore()

# ---- helpers ----
def deterministic_shard(items: List[str], idx: int) -> List[str]:
    """Return items where hash(slug) % SHARD_TOTAL == idx."""
    shard_items = []
    for item in items:
        # item is relative path inside repo, e.g. "batches/raw/abc/xyz.parquet"
        slug = item
        h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
        if h % SHARD_TOTAL == idx:
            shard_items.append(item)
    return shard_items

def load_manifest() -> List[str]:
    """Load manifest from repo (cached locally if possible)."""
    local_manifest = Path("cache") / MANIFEST_PATH
    if local_manifest.exists():
        return json.loads(local_manifest.read_text())

    # Download manifest via CDN (no auth)
    url = f"{BASE_CDN}/{MANIFEST_PATH}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Manifest not found at {url}: {r.status_code}")

    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(r.content)
    return json.loads(r.content)

def project_to_pair(raw_obj: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous file content to {prompt, response}.
    Supports common patterns seen in surrogate-1 repos.
    """
    # If already pair-like, return as-is
    if "prompt" in raw_obj and "response" in raw_obj:
        return {"prompt": str(raw_obj["prompt"]), "response": str(raw_obj["response"])}

    # Common aliases
    prompt_keys = ["prompt", "input", "question", "instruction", "text"]
    response_keys = ["response", "output", "answer", "completion", "target"]

    prompt = None
    response = None
    for k in prompt_keys:
        if k in raw_obj:
            prompt = raw_obj[k]
            break
    for k in response_keys:
        if k in raw_obj:
            response = raw_obj[k]
            break

    # Fallback: if exactly two fields, treat as (prompt, response)
    if prompt is None and response is None and isinstance(raw_obj, dict) and len(raw_obj) == 2:
        vals = list(raw_obj.values())
        prompt, response = str(vals[0]), str(vals[1])

    if prompt is None or response is None:
        raise ValueError(f"Cannot project object to pair: {raw_obj}")

    return {"prompt": str(prompt), "response": str(response)}

def parse_parquet_cdn(path: str) -> List[Dict[str, str]]:
    """Download parquet via CDN and parse to pairs (avoids HF datasets streaming)."""
    import pyarrow.parquet as pq
    import pyarrow as pa
    import io

    url = f"{BASE_CDN}/{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    buf = io.BytesIO(r.content)
    table = pq.read_table(buf)

    pairs = []
    # Convert to list of dicts
    cols = table.column_names
    for i in range(table.num_rows):
        row = {col: table[col][i].as_py() for col in cols}
        try:
            pair = project_to_pair(row)
            pairs.append(pair)
        except ValueError:
            # skip unprojectable rows
            continue
    return pairs

def parse_jsonl_cdn(path: str) -> List[Dict[str, str]]:
    url = f"{BASE_CDN}/{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    pairs = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            pair = project_to_pair(obj)
            pairs.append(pair)
        except (json.JSONDecodeError, ValueError):
            continue
    return pairs

def parse_file(path: str) -> List[Dict[str, str]]:
    if path.endswith(".parquet"):
        return parse_parquet_cdn(path)
    elif path.endswith(".jsonl"):
        return parse_jsonl_cdn(path)
    else:
        raise ValueError(f"Unsupported file type: {path}")

def upload_shard(output_path: Path, remote_path: str):
    """Upload file to HF Hub (single commit per shard)."""
    # Use HF API to upload
    API.upload_file(
        path_or_fileobj=str(output_path),
        path_in_repo=remote_path,
        repo_id=
