# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed manifest** (`manifest/<DATE_FOLDER>.json` or `MANIFEST_URL`) produced by the Mac orchestrator to avoid recursive HF API calls and rate limits.
- Each shard deterministically hashes the **full filename** (not just slug) → `bucket = int(sha256(filename)[:16], 16) % SHARD_TOTAL` and only processes files where `bucket == SHARD_ID`.
- Downloads qualifying files via **HF CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header — completely bypasses `/api/` rate limits.
- Projects each file to `{prompt, response}` only at parse time (avoids `pyarrow.CastError` on mixed schemas).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Writes output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` and pushes to HF dataset repo using `huggingface_hub` (single commit per shard).
- Reuses existing GitHub Actions matrix setup; only the worker script changes.

---

### 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
Env:
  SHARD_ID            - required, 0..15
  SHARD_TOTAL         - optional, default 16
  DATE_FOLDER         - optional, default today YYYY-MM-DD
  HF_TOKEN            - write token for axentx/surrogate-1-training-pairs
  MANIFEST_URL        - optional, URL to precomputed file list JSON
  DATASET_REPO        - optional, default axentx/surrogate-1-training-pairs
"""

import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent / "lib"))
import dedup  # noqa: E402

# ---- config ----
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.datetime.utcnow().strftime("%Y-%m-%d"))

# Where to put local temp files
WORKDIR = Path("/tmp/surrogate_ingest")
WORKDIR.mkdir(parents=True, exist_ok=True)

# ---- helpers ----
def hf_api() -> HfApi:
    return HfApi(token=HF_TOKEN)

def list_files_via_manifest() -> List[str]:
    """
    Prefer precomputed manifest to avoid recursive HF API calls.
    Fallback: single-level list_repo_tree for DATE_FOLDER only.
    """
    # 1) Try local manifest file first
    local_manifest = Path(__file__).parent.parent / "manifest" / f"{DATE_FOLDER}.json"
    if local_manifest.exists():
        with local_manifest.open() as f:
            data = json.load(f)
            if isinstance(data, dict) and "files" in data:
                return data["files"]
            return data

    # 2) Try remote manifest URL
    manifest_url = os.getenv("MANIFEST_URL")
    if manifest_url:
        r = requests.get(manifest_url, timeout=60)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "files" in data:
            return data["files"]
        return data

    # 3) Fallback: list one folder level (non-recursive) via HF API
    # This is a single call per shard (all shards share same folder).
    api = hf_api()
    tree = api.list_repo_tree(
        repo_id=DATASET_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    return files

def deterministic_shard(files: List[str], shard_id: int, shard_total: int) -> List[str]:
    """Stable shard assignment by full filename hash."""
    hashed = [(hashlib.sha256(f.encode()).hexdigest(), f) for f in files]
    hashed.sort(key=lambda x: x[0])
    return [f for h, f in hashed if int(h[:16], 16) % shard_total == shard_id]

def cdn_download(repo: str, rfilename: str, dest: Path) -> Path:
    """Download via HF CDN (no auth header, bypasses /api/ rate limits)."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{rfilename}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest

def project_to_pair(raw_path: Path) -> List[Dict[str, str]]:
    """
    Project file to {prompt, response} only.
    Supports .jsonl and .parquet (common in surrogate-1).
    """
    suffix = raw_path.suffix.lower()
    if suffix == ".jsonl":
        try:
            import jsonlines
        except ImportError:
            print("ERROR: jsonlines not installed", file=sys.stderr)
            return []
        pairs = []
        with jsonlines.open(raw_path) as reader:
            for obj in reader:
                prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
                response = obj.get("response") or obj.get("output") or obj.get("completion")
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            print("ERROR: pyarrow not installed", file=sys.stderr)
            return []
        table = pq.read_table(raw_path)
        cols = table.column_names
        prompt_col = next((c for c in ("prompt", "input", "text") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
        if not prompt_col or not response_col:
            return []
        prompts = table.column(prompt_col).to_pylist()
        responses = table.column(response_col).to_pylist()
        return [
            {"prompt": str(p), "response": str(r)}
            for p, r in zip(prompts, responses)
            if p is not None and r is not None
        ]

    return []

def upload_shard(output_path: Path, date_folder: str, shard_id: int) -> None:
    """Upload shard file to dataset repo under batches/public-merged/<date>/"""
    api = hf_api()
    remote_path = f"batches/public-merged/{date_folder}/{output_path.name}"
    api.upload_file(
        path_or_fileobj=str(output_path),
        path_in_repo=remote_path,
        repo_id=DATASET_REPO,
        repo_type="dataset",
        commit_message=f"shard {shard_id} public-merged {date_folder}",
    )
    print(f"Uploaded {remote_path}")

# ---- main ----
def main() -> None:
    print(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for {DATE_FOLDER}")

    files = list_files_via_manifest()
    print(f"Total files in scope: {len(files)}")
    shard_files = deterministic_shard(files, SHARD_ID, SHARD_TOTAL)
    print(f"Shard {SHARD_ID} assigned {len(shard_files
