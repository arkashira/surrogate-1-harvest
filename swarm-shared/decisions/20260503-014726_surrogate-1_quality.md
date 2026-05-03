# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date and committed to the repo (or passed via env) — avoids recursive `list_repo_files` and HF API rate limits.
- Downloads **only assigned shard files** via **CDN direct URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero Authorization header, bypasses `/api/` rate limits.
- Projects each file to `{prompt, response, hash}` at parse time — handles mixed schemas without `load_dataset(streaming=True)` pyarrow CastError.
- Dedups via central `lib/dedup.py` SQLite store (WAL mode) and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Runs as a single Python script executable in GitHub Actions matrix (`shard_id`, `total_shards`).

---

### Steps (≤2h)

1. **Create `bin/dataset-enrich.py`** (replaces `dataset-enrich.sh`)
   - Accept `SHARD_ID`, `TOTAL_SHARDS`, `DATE`, `MANIFEST_PATH` (or repo+path).
   - Load manifest JSON (local file or fallback to `list_repo_tree` once per shard if missing).
   - Deterministic shard assignment: `hash(slug) % TOTAL_SHARDS == SHARD_ID`.
   - For each assigned file:
     - Build CDN URL and stream download via `requests.get(..., stream=True)`.
     - Parse line-by-line (JSONL/JSON/parquet via `pyarrow` as needed) and project `{prompt, response, hash}`.
     - Compute md5 of canonical content; skip if already in central dedup store.
     - Collect valid pairs.
   - Write output to `batches/public-merged/<DATE>/shard<SHARD_ID>-<TS>.jsonl`.
   - Push to HF dataset repo via `huggingface_hub` upload (single commit per shard).

2. **Update `lib/dedup.py`**
   - Ensure thread/process-safe SQLite access (WAL mode) for concurrent CI runners.
   - Expose `claim(content_hash: str) -> bool` (atomic insert-or-ignore).

3. **Update `.github/workflows/ingest.yml`**
   - Pass matrix `shard_id` and `total_shards` to the Python script.
   - Set `SHELL=/bin/bash` and ensure `HF_TOKEN` is available.
   - Use `actions/setup-python` and install `requirements.txt`.

4. **Add orchestrator helper (optional, Mac-side)**
   - Small script to generate `manifest-<DATE>.json` via `list_repo_tree` and commit to repo (or upload as artifact) — keeps CI workers zero-API.

5. **Test locally**
   - Dry-run one shard against a small date folder; verify output schema and dedup behavior.

---

### Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 DATE=2026-05-03 \
  python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --path raw/2026-05-03 \
    --manifest manifests/2026-05-03.json \
    --out-dir batches/public-merged

If --manifest is provided and exists, uses it. Otherwise, each shard will
perform a single non-recursive list_repo_tree for the given path (shared
across shards) and cache it locally.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi
from tqdm import tqdm

from lib.dedup import DedupStore  # exposes DedupStore with claim(hash) -> bool

HF_API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_WAIT = 360  # seconds after 429
MAX_RETRIES = 3

def deterministic_shard(slug: str, total_shards: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total_shards

def load_manifest(manifest_path: Optional[str], repo: str, path: str) -> List[str]:
    if manifest_path and Path(manifest_path).exists():
        with open(manifest_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("files", [])
    # fallback: single non-recursive tree list (shared by all shards)
    try:
        tree = HF_API.list_repo_tree(repo=repo, path=path, recursive=False)
        files = [item.path for item in tree if item.type == "file"]
        if manifest_path:
            Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
            with open(manifest_path, "w") as f:
                json.dump(files, f)
        return files
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
        return []

def stream_cdn(url: str, headers: Optional[Dict[str, str]] = None) -> Iterable[bytes]:
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 429:
                    wait = RETRY_WAIT
                    print(f"Rate limited (429). Waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    yield chunk
            return
        except (requests.RequestException, OSError) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt
            print(f"Download error ({e}), retry {attempt+1}/{MAX_RETRIES} in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("Unreachable")

def canonical_json_bytes(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

def extract_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Map varied schemas to {prompt, response, hash}."""
    prompt = None
    response = None

    # common field names
    for pkey in ("prompt", "instruction", "input", "question", "user"):
        if pkey in obj and isinstance(obj[pkey], str) and obj[pkey].strip():
            prompt = obj[pkey].strip()
            break
    for rkey in ("response", "output", "answer", "assistant", "completion"):
        if rkey in obj and isinstance(obj[rkey], str) and obj[rkey].strip():
            response = obj[rkey].strip()
            break

    if not prompt or not response:
        return None

    content = {"prompt": prompt, "response": response}
    h = hashlib.md5(canonical_json_bytes(content)).hexdigest()
    content["hash"] = h
    return content

def parse_file_to_pairs(repo: str, file_path: str, dedup: DedupStore) -> List[Dict[str, str]]:
    """Download via CDN and project to {prompt, response, hash}. Returns valid pairs."""
    url = CDN_TEMPLATE.format(repo=repo, path=file_path)
    suffix = Path(file_path).suffix.lower()
    pairs = []

    if suffix == ".jsonl":
        buffer = b""
        for chunk in stream_cdn(url):
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines[-1]
            for line in lines[:-1]:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pair = extract_pair(obj)
                if pair and dedup.
