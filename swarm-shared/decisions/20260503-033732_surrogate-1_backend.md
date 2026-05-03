# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **one-time API call** from the runner (or pre-baked manifest) to list files in `{DATE_FOLDER}/` via `list_repo_tree(recursive=False)` → saves `manifest-{DATE_FOLDER}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads assigned files **via CDN** (`https://huggingface.co/datasets/{owner}/{repo}/resolve/main/{path}`) with no Authorization header (bypasses 429 API limits)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central `lib/dedup.py` md5 store (same as Space)
- Writes output to `batches/public-merged/{DATE_FOLDER}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Commits via HF API with deterministic filename to avoid collisions

### Steps (1h 30m)

1. (10m) Create `bin/dataset-enrich.py` with CLI args and CDN downloader
2. (20m) Implement manifest loader + shard assignment + streaming JSONL writer
3. (20m) Integrate `lib/dedup.py` and schema projection (prompt/response only)
4. (20m) Add HF commit logic (deterministic filename, retries on 429 with 360s backoff)
5. (20m) Update `.github/workflows/ingest.yml` to use `python bin/dataset-enrich.py` and pass matrix vars
6. (10m) Make executable, test locally with `HF_TOKEN` and dry-run flag

---

## `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date-folder 2026-05-03 \
    --out-dir batches/public-merged

Environment:
  HF_TOKEN         required for commit (write)
  DRY_RUN          if set, skip upload and print actions
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.json as paj
import requests
from huggingface_hub import HfApi, list_repo_tree

# --
# Config
# --
CDN_TEMPLATE = "https://huggingface.co/datasets/{owner}/{repo}/resolve/main/{path}"
API_RATE_LIMIT_RETRY = 360  # seconds (per pattern)
MAX_RETRIES = 5
BATCH_SIZE = 500  # rows per JSONL write chunk

# --
# Dedup store (shared with HF Space)
# --
sys.path.insert(0, str(Path(__file__).parent / "lib"))
try:
    from dedup import DedupStore
except Exception:
    # Fallback minimal dedup if lib unavailable in runner context
    class DedupStore:
        def __init__(self, db_path: str = ":memory:"):
            self.seen = set()

        def exists(self, md5: str) -> bool:
            return md5 in self.seen

        def add(self, md5: str) -> None:
            self.seen.add(md5)

# --
# Helpers
# --
def deterministic_shard(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

def load_manifest(repo: str, date_folder: str, token: Optional[str]) -> List[str]:
    """List files in date_folder via HF API (single call)."""
    api = HfApi(token=token)
    try:
        tree = list_repo_tree(repo=repo, path=date_folder, recursive=False, token=token)
    except Exception as e:
        # If API fails, allow manifest file fallback
        manifest_path = Path(f"manifest-{date_folder}.json")
        if manifest_path.exists():
            with open(manifest_path) as f:
                return [line.strip() for line in f if line.strip()]
        raise RuntimeError(f"Failed to list repo tree and no manifest-{date_folder}.json: {e}")

    files = [item.rfilename for item in tree if item.type == "file"]
    # Save for reproducibility / debugging
    Path(f"manifest-{date_folder}.json").write_text("\n".join(files))
    return files

def cdn_download(url: str, timeout: int = 30) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            print(f"[WARN] CDN download failed ({e}), retry {attempt}/{MAX_RETRIES} in {wait}s", file=sys.stderr)
            time.sleep(wait)

def project_to_pair(raw_bytes: bytes, filename: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response} only.
    Supports JSONL, JSON, and parquet projections.
    """
    name = filename.lower()
    try:
        if name.endswith(".jsonl"):
            lines = raw_bytes.decode().strip().splitlines()
            rows = [json.loads(l) for l in lines if l.strip()]
        elif name.endswith(".json"):
            rows = json.loads(raw_bytes.decode())
            if not isinstance(rows, list):
                rows = [rows]
        elif name.endswith(".parquet"):
            table = paj.read_table(pa.BufferReader(raw_bytes))
            rows = table.to_pylist()
        else:
            print(f"[WARN] Unknown file type {filename}, skipping", file=sys.stderr)
            return None
    except Exception as e:
        print(f"[WARN] Cannot parse {filename}: {e}", file=sys.stderr)
        return None

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prompt = row.get("prompt") or row.get("input") or row.get("question")
        response = row.get("response") or row.get("output") or row.get("answer")
        if prompt is None or response is None:
            continue
        out.append({"prompt": str(prompt), "response": str(response)})
    return out

def hf_commit(
    repo: str,
    path: str,
    content: bytes,
    token: str,
    commit_message: str,
) -> None:
    api = HfApi(token=token)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            api.upload_file(
                path_or_fileobj=content,
                path_in_repo=path,
                repo_id=repo,
                repo_type="dataset",
                commit_message=commit_message,
            )
            print(f"[INFO] Uploaded {path}")
            return
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                if attempt == MAX_RETRIES:
                    raise
                print(f"[WARN] HF 429 rate-limited, waiting {API_RATE_LIMIT_RETRY}s", file=sys.stderr)
                time.sleep(API_RATE_LIMIT_RETRY)
                continue
            raise

# --
# Main worker
# --
def main() -> None:
    parser = argparse.ArgumentParser(description="surrogate-1 CDN-bypass ingestion worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs", help="HF dataset repo")
    parser.add_argument("--date-folder", default=datetime.utcnow().strftime("%Y-%m-%d"), help="Date folder in repo")
    parser.add_argument("--out-dir", default="batches/public-merged", help="
