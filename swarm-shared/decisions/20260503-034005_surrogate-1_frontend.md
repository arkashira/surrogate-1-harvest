# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single API call to `list_repo_tree` for the date folder → saves `manifest.json` → workers use CDN URLs only (zero API calls during streaming)
- Projects heterogeneous HF datasets to `{prompt, response}` at parse time (avoids `pyarrow.CastError`)
- Deterministic shard assignment via `hash(slug) % SHARD_TOTAL`
- Central dedup via existing `lib/dedup.py` (SQLite md5 store)
- Outputs `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with newline JSON `{prompt, response, source_file, sha256}`
- Includes retry/backoff for CDN downloads and 429 handling (wait 360s)

---

### 1) New file: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03 python bin/dataset-enrich.py

Environment:
  HF_TOKEN         - HuggingFace write token (for upload)
  REPO_ID          - dataset repo (default: axentx/surrogate-1-training-pairs)
  SHARD_ID         - 0..SHARD_TOTAL-1
  SHARD_TOTAL      - default 16
  DATE_FOLDER      - default today YYYY-MM-DD
"""

import os
import sys
import json
import time
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ── config --
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
WORK_DIR = Path(__file__).parent.parent
DEDUP_PY = WORK_DIR / "lib" / "dedup.py"
OUTPUT_DIR = WORK_DIR / "batches" / "public-merged" / DATE_FOLDER
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── helpers --
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def backoff(attempt: int) -> float:
    return min(60.0, (2 ** attempt) + (attempt * 0.5))

def download_cdn(url: str, timeout: int = 30) -> bytes:
    for attempt in range(8):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 429:
                wait = 360
                print(f"CDN 429, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.content
        except Exception as exc:
            wait = backoff(attempt)
            print(f"Download failed ({exc}), retry {attempt+1}/8 in {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download after retries: {url}")

# ── manifest --
def build_manifest(date_folder: str) -> List[Dict]:
    """
    Single API call: list top-level folder for date.
    Returns list of dict: {path, size}
    """
    print(f"Listing repo tree for {REPO_ID}/{date_folder} ...", file=sys.stderr)
    try:
        tree = list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            recursive=False,
            token=HF_TOKEN,
        )
    except Exception as exc:
        print(f"Failed to list repo tree: {exc}", file=sys.stderr)
        # Fallback: try to read existing manifest if present
        manifest_path = WORK_DIR / "manifests" / date_folder / "manifest.json"
        if manifest_path.exists():
            print(f"Using cached manifest: {manifest_path}", file=sys.stderr)
            return json.loads(manifest_path.read_text())
        raise

    items = []
    for entry in tree:
        if entry.type == "file":
            items.append({"path": entry.path, "size": getattr(entry, "size", 0)})

    # cache manifest
    manifest_path = WORK_DIR / "manifests" / date_folder / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(items, indent=2))
    print(f"Manifest saved ({len(items)} files) -> {manifest_path}", file=sys.stderr)
    return items

# ── projection --
def project_to_pair(raw_bytes: bytes, path: str) -> Optional[Tuple[str, str]]:
    """
    Best-effort projection to {prompt, response}.
    Supports:
      - JSON/JSONL lines with common key variants
      - Parquet via pyarrow (streaming-safe)
    Returns None if cannot extract.
    """
    path_l = path.lower()
    suffix = Path(path).suffix.lower()

    # Parquet: use pyarrow to read only required columns
    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            import pyarrow as pa
            # Read only metadata to get schema first (cheap)
            pf = pq.ParquetFile(pa.BufferReader(raw_bytes))
            schema = pf.schema.names
            # Find candidate columns
            prompt_col = None
            response_col = None
            for c in schema:
                cl = c.lower()
                if "prompt" in cl and not prompt_col:
                    prompt_col = c
                if cl in ("response", "completion", "answer", "output") and not response_col:
                    response_col = c
            if not prompt_col or not response_col:
                return None
            table = pf.read(columns=[prompt_col, response_col])
            # Take first row as representative sample for this worker's slice
            # (caller should iterate rows if needed; here we sample one)
            df = table.to_pandas()
            if len(df) == 0:
                return None
            row = df.iloc[0]
            return str(row[prompt_col]), str(row[response_col])
        except Exception as exc:
            print(f"Parquet projection failed for {path}: {exc}", file=sys.stderr)
            return None

    # JSON/JSONL
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    prompt_keys = {"prompt", "instruction", "input", "question", "query"}
    response_keys = {"response", "completion", "answer", "output", "generation"}

    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            try:
                obj = json.loads(text)
            except Exception:
                continue

        if isinstance(obj, dict):
            pk = next((k for k in obj if k.lower() in prompt_keys), None)
            rk = next((k for k in obj if k.lower() in response_keys), None)
            if pk and rk:
                return str(obj[pk]), str(obj[rk])

            # fallback: first string-like pair
            str_vals = [v for v
