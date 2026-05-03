# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date and committed to the repo (or passed via `MANIFEST_URL`).  
- During the 30-minute cron run, each shard **never calls `list_repo_files` or any HF API** — it only downloads via CDN (`resolve/main/...`) using the pre-computed manifest.  
- Projects heterogeneous files to `{prompt, response}` at parse time (no schema merge).  
- Deduplicates via the existing `lib/dedup.py` md5 store and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.  
- Keeps the shell wrapper for GitHub Actions matrix compatibility but delegates to Python with proper shebang and `set -euo pipefail`.

### Steps (est. 90 min)

1. Create `bin/dataset-enrich.py` (manifest + CDN fetcher + shard filtering + projection + dedup + upload).  
2. Update `bin/dataset-enrich.sh` to a thin Bash wrapper that invokes `python bin/dataset-enrich.py "$@"`.  
3. Add `MANIFEST_PATH`/`MANIFEST_URL` env var support (fallback to repo tree via HF API only on Mac/local dev).  
4. Ensure executable bits and shebang (`#!/usr/bin/env python3`).  
5. Quick smoke test with a small manifest subset.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Each GitHub Actions shard (SHARD_ID 0-15, TOTAL_SHARDS=16) processes only
the deterministic slice of files listed in MANIFEST_PATH (JSON).

Manifest format:
{
  "date": "2026-05-03",
  "repo": "datasets/axentx/surrogate-1-training-pairs",
  "files": [
    {"path": "batches/raw/abc/xyz.parquet", "size": 12345, "md5": "..."},
    ...
  ]
}

Behavior:
- Downloads via CDN (no Authorization header) to bypass HF API rate limits.
- Projects each file to {prompt, response} at parse time.
- Deduplicates via lib.dedup.Md5Store.
- Outputs batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
- Uploads to HF dataset repo via huggingface_hub.
"""

import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import Md5Store  # noqa: E402

# Env config
HF_TOKEN = os.getenv("HF_TOKEN")
REPO_ID = os.getenv("REPO_ID", "datasets/axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest/latest.json")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
CDN_BASE = "https://huggingface.co"

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
API = HfApi(token=HF_TOKEN)


def load_manifest(path: str) -> Dict[str, Any]:
    if path.startswith("http://") or path.startswith("https://"):
        resp = requests.get(path, timeout=30)
        resp.raise_for_status()
        return resp.json()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def shard_filter(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic shard assignment by file path hash."""
    assigned = []
    for f in files:
        h = int(hashlib.sha256(f["path"].encode()).hexdigest(), 16)
        if h % TOTAL_SHARDS == SHARD_ID:
            assigned.append(f)
    return assigned


def cdn_download_url(repo: str, filepath: str) -> str:
    # repo format: datasets/owner/name
    parts = repo.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid repo format: {repo}")
    _, repo_path = parts
    return f"{CDN_BASE}/{repo_path}/resolve/main/{filepath}"


def safe_download(url: str, timeout: int = 60) -> bytes:
    # CDN downloads do NOT require Authorization header and bypass API rate limits.
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def project_to_pair(data: Any, filepath: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response}.
    Supports:
    - Parquet with any schema: looks for prompt/response fields (case-insensitive).
    - JSON/JSONL objects with prompt/response keys.
    Returns None if projection fails.
    """
    ext = Path(filepath).suffix.lower()
    try:
        if ext == ".parquet":
            table = pq.read_table(pa.BufferReader(data))
            cols = {c.lower(): c for c in table.column_names}
            prompt_col = cols.get("prompt") or cols.get("input") or cols.get("question")
            response_col = cols.get("response") or cols.get("output") or cols.get("answer")
            if not prompt_col or not response_col:
                return None
            # Take first row as representative; in practice you may stream rows.
            df = table.to_pandas()
            if len(df) == 0:
                return None
            return {"prompt": str(df.iloc[0][prompt_col]), "response": str(df.iloc[0][response_col])}

        # Fallback: try JSON
        if ext in (".json", ".jsonl"):
            obj = json.loads(data)
            if isinstance(obj, list):
                obj = obj[0] if obj else None
            if isinstance(obj, dict):
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt is not None and response is not None:
                    return {"prompt": str(prompt), "response": str(response)}
    except Exception as exc:
        print(f"Projection failed for {filepath}: {exc}", file=sys.stderr)
    return None


def upload_chunk(output_path: str, repo_id: str, commit_message: str) -> None:
    API.upload_file(
        path_or_fileobj=output_path,
        path_in_repo=output_path,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=commit_message,
    )


def main() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    date_str = manifest.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    files = manifest.get("files", [])
    if not files:
        print("No files in manifest; exiting.", file=sys.stderr)
        return

    my_files = shard_filter(files)
    print(f"Shard {SHARD_ID}/{TOTAL_SHARDS}: processing {len(my_files)} files")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    dedup = Md5Store()

    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_name = f"shard{SHARD_ID}-{timestamp}.jsonl"
    out_path = Path(OUTPUT_DIR) / out_name
    remote_path = f"batches/public-merged/{date_str}/{out_name}"

    processed = 0
    skipped_dup = 0
    failed = 
