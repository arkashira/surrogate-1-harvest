# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` per date folder → save manifest JSON
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via **HF CDN bypass** (`resolve/main/...`) — zero API calls during data fetch
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Writes `batches/public-merged/<DATE>/shard<N>-<HHMMSS>.jsonl`
- Uses central `lib/dedup.py` for cross-source md5 dedup (same interface as existing)
- Exits 0 on success, non-zero on hard failure (GitHub Actions will retry)

### Why this is the highest-value incremental improvement
- Fixes the **HF API 429/1000 req/5min** and **commit cap 128/hr** bottlenecks by moving to CDN-only fetches and deterministic sharding
- Eliminates `load_dataset(streaming=True)` schema crashes by per-file projection
- Keeps runtime <2h by reusing existing dedup lib and minimal interface changes
- Workflow file only needs a one-line script path change

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Env:
  SHARD_ID        (int) 0..15
  SHARD_TOTAL=16  (int)
  DATE            (str) YYYY-MM-DD
  HF_TOKEN        (str) write token for axentx/surrogate-1-training-pairs
  REPO_OWNER=axentx
  REPO_NAME=surrogate-1-training-pairs
  MANIFEST_PATH   (optional) path to pre-saved manifest.json
"""
import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

import requests
import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np

# ── config ──────────────────────────────────────────────────────────────
REPO_OWNER = os.getenv("REPO_OWNER", "axentx")
REPO_NAME = os.getenv("REPO_NAME", "surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "")

BASE_CDN = f"https://huggingface.co/datasets/{REPO_OWNER}/{REPO_NAME}/resolve/main"
BASE_API = f"https://huggingface.co/api/datasets/{REPO_OWNER}/{REPO_NAME}"

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

# ── dedup helper ───────────────────────────────────────────────────────
def _load_dedup_db():
    # delegate to existing lib/dedup.py if present; otherwise lightweight local sqlite
    lib_path = Path(__file__).parent / "lib" / "dedup.py"
    if lib_path.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("dedup", str(lib_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.DedupStore()
    # fallback: simple in-memory set (cross-run duplicates possible — acceptable trade-off)
    class _EphemeralDedup:
        def __init__(self):
            self.seen = set()
        def exists(self, key):
            return key in self.seen
        def add(self, key):
            self.seen.add(key)
    return _EphemeralDedup()

dedup = _load_dedup_db()

# ── helpers ────────────────────────────────────────────────────────────
def hf_api_get(path: str, params: Dict = None, retries: int = 3) -> Any:
    url = f"{BASE_API}/{path}"
    for attempt in range(retries):
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if r.status_code == 429:
            wait = 360
            print(f"[WARN] HF API 429, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"HF API failed after {retries} retries: {path}")

def list_date_files(date: str) -> List[str]:
    """List parquet/jsonl files under <date>/ (non-recursive)."""
    manifest_file = Path(MANIFEST_PATH) if MANIFEST_PATH else Path(f"manifest-{date}.json")
    if manifest_file.exists():
        with open(manifest_file) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "files" in data:
            return data["files"]

    # single API call: list top-level of date folder only
    items = hf_api_get(f"tree/{date}", params={"recursive": False})
    files = [it["path"] for it in items if it.get("type") == "file"]
    # save for reuse by other shards / retries
    with open(manifest_file, "w") as f:
        json.dump(files, f)
    return files

def belongs_to_shard(slug: str) -> bool:
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return (h % SHARD_TOTAL) == SHARD_ID

def download_via_cdn(path: str) -> bytes:
    url = f"{BASE_CDN}/{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def project_to_pair(raw: Dict) -> Dict:
    """Normalize heterogeneous schemas to {prompt, response}."""
    # Common field names seen in public datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "completion", "output", "answer"}

    pk = None
    rk = None
    for k in raw:
        if pk is None and k.lower() in prompt_keys:
            pk = k
        if rk is None and k.lower() in response_keys:
            rk = k

    prompt = raw.get(pk) if pk else (raw.get("prompt") or raw.get("instruction") or "")
    response = raw.get(rk) if rk else (raw.get("response") or raw.get("completion") or "")

    # coerce to string
    prompt = "" if prompt is None else str(prompt).strip()
    response = "" if response is None else str(response).strip()

    if not prompt and not response:
        # fallback: serialize entire object into prompt for manual review
        prompt = json.dumps(raw, ensure_ascii=False)

    return {"prompt": prompt, "response": response}

def parse_file(content: bytes, path: str) -> List[Dict]:
    """Parse parquet/jsonl/json and yield projected pairs."""
    suffix = Path(path).suffix.lower()
    out = []

    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(content))
            df = table.to_pandas()
            for _, row in df.iterrows():
                raw = row.to_dict()
                pair = project_to_pair(raw)
                if pair["prompt"] or pair["response"]:
                    out.append(pair)

        elif suffix in (".jsonl", ".ndjson"):
            for line in content.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                pair = project_to_pair(raw)
                if pair["prompt"] or pair["response"]:
                    out.append(pair)

        elif suffix == ".json":
            raw = json.loads(content.decode("utf-8", errors="replace
