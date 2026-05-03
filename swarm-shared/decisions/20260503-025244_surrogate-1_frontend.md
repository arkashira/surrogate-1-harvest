# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list saved to `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization header during data fetch (uses token only for repo metadata/list)
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` pyarrow CastError)
- Dedups via central `lib/dedup.py` md5 store
- Outputs: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- **Exit codes**: 0 on success, non-zero on fatal error (GitHub Actions-friendly)
- Mac orchestration only; heavy compute/ingest stays in CI runners

---

## Changes

### 1) `bin/dataset-enrich.py` (new)
```python
#!/usr/bin/env python3
"""
CDN-bypass, manifest-driven ingestion worker for surrogate-1.

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Behavior:
- list_repo_tree(path=DATE, recursive=False) once -> manifest-{DATE}.json
- deterministic shard by hash(slug) % SHARD_TOTAL
- downloads via HF CDN (no auth header) to bypass API rate limits
- projects heterogeneous files to {prompt, response}
- dedups via lib.dedup
- writes batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
- exits 0 on success, non-zero on fatal error
"""

import os
import sys
import json
import hashlib
import time
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from huggingface_hub import HfApi

# ── config --
REPO_DATASET = "axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT_ROOT = Path("batches/public-merged")
MANIFEST_PATH = Path(f"manifest-{DATE}.json")
API = HfApi(token=HF_TOKEN)

# ── dedup --
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa

dedup = DedupStore()

# ── helpers --
def deterministic_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def cdn_url(path: str) -> str:
    # Public CDN URL — no Authorization header required
    return f"https://huggingface.co/datasets/{REPO_DATASET}/resolve/main/{path}"

def safe_get(url: str, headers: Optional[Dict[str, str]] = None, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = 360
                print(f"[cdn] 429 rate-limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == retries - 1:
                raise
            sleep_sec = 2 ** attempt
            print(f"[cdn] retry {attempt+1}/{retries} after {sleep_sec}s: {exc}", file=sys.stderr)
            time.sleep(sleep_sec)
    raise RuntimeError("unreachable")

def list_date_files(date_folder: str) -> List[str]:
    """
    Single API call to list files in DATE folder (non-recursive).
    Avoids recursive list_repo_files pagination and rate limits.
    """
    items = API.list_repo_tree(repo_id=REPO_DATASET, path=date_folder, recursive=False)
    files = []
    for item in items:
        if item.get("type") == "file":
            files.append(f"{date_folder}/{item['path']}")
    return files

def parse_file_to_pairs(content: bytes, suffix: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response} only.
    Supports .jsonl and .parquet from raw bytes.
    """
    suffix = suffix.lower()
    pairs = []

    if suffix == ".parquet":
        try:
            table = pq.read_table(pa.BufferReader(content), columns=["prompt", "response"])
        except (pa.ArrowInvalid, KeyError, OSError):
            # fallback: read all and project
            table = pq.read_table(pa.BufferReader(content))
            cols = table.column_names
            prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer") if c in cols), None)
            if prompt_col is None or response_col is None:
                return []
            table = table.select([prompt_col, response_col]).rename_columns(["prompt", "response"])

        df = table.to_pandas()
        for _, row in df.iterrows():
            p = str(row.get("prompt") or "").strip()
            r = str(row.get("response") or "").strip()
            if p and r:
                pairs.append({"prompt": p, "response": r})
        return pairs

    # assume jsonl
    text = content.decode("utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = str(obj.get("prompt") or obj.get("input") or obj.get("question") or "").strip()
        r = str(obj.get("response") or obj.get("output") or obj.get("answer") or "").strip()
        if p and r:
            pairs.append({"prompt": p, "response": r})
    return pairs

# ── main --
def main() -> int:
    if not HF_TOKEN:
        print("HF_TOKEN is required", file=sys.stderr)
        return 1

    print(f"[worker] SHARD_ID={SHARD_ID}/{SHARD_TOTAL} DATE={DATE}")

    # 1) manifest: single API call
    if MANIFEST_PATH.exists():
        print(f"[worker] reusing existing manifest: {MANIFEST_PATH}")
        with open(MANIFEST_PATH) as f:
            file_paths = json.load(f)
    else:
        print(f"[worker] listing repo tree for {DATE}...")
        file_paths = list_date_files(DATE)
        with open(MANIFEST_PATH, "w") as f:
            json.dump(file_paths, f)
        print(f"[worker] manifest saved: {len(file_paths)} files")

    # 2) deterministic shard selection
    my_files = [p for p in file_paths if deterministic_shard(p) == SHARD_ID]
    print(f"[worker] assigned {len(my_files)} files for shard {SHARD_ID}")

    # 3) process
    out_dir = OUTPUT_ROOT / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    accepted = 0
    skipped_dup = 0
    processed_files = 0

    for rel_path in my_files:
        try:
            # download via CDN (no auth header) to bypass API rate limits
            url = cdn_url(rel_path)
            data = safe_get(url
