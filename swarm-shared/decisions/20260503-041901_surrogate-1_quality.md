# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `bin/worker.py` — single-file, manifest-driven worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` from the matrix job
   - Uses one HF API call (from the runner) to list a **date folder** in `axentx/surrogate-1-training-pairs/batches/public-merged/<date>/`
   - Persists that file list to `manifest.json`
   - Downloads **only files assigned to this shard** via raw CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) — zero auth, zero API rate limits during training
   - Streams each file with `datasets` but projects to `{prompt, response}` immediately (avoids mixed-schema CastError)
   - Deduplicates via `lib/dedup.py` (central md5 store)
   - Emits `shard<N>-<HHMMSS>.jsonl` to `batches/public-merged/<date>/`

2. Update `bin/dataset-enrich.sh` → thin wrapper that invokes `python bin/worker.py` with proper env and retries.

3. Update `.github/workflows/ingest.yml`:
   - Add step to compute `DATE` (UTC) once and pass to all shards
   - Ensure `python -m pip install -r requirements.txt` runs before worker
   - Keep 16-shard matrix; each shard remains isolated (7 GB)

4. Update `requirements.txt` to include `requests` (for CDN) if not present.

### Why this is highest value
- Eliminates HF API rate limits during data loading (CDN bypass)
- Prevents `pyarrow.CastError` from heterogeneous repo files by projecting schema early
- Manifest-driven approach means Lightning training scripts can reuse the same file list and do **zero API calls** during training
- Keeps runner isolation (16×7 GB) while making ingestion robust and reproducible

---

## Code Snippets

### 1. `bin/worker.py` (new)

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Environment:
  SHARD_ID:        0..15
  TOTAL_SHARDS:    16
  HF_TOKEN:        write token for axentx/surrogate-1-training-pairs
  DATE:            YYYY-MM-DD (UTC) — target folder under batches/public-merged/
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from datasets import load_dataset, Dataset
from huggingface_hub import HfApi, list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MANIFEST_PATH = Path("manifest.json")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Import local dedup (must be runnable from repo root)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, mark_seen  # type: ignore

HF_API_RETRY_WAIT = 360  # seconds after 429


def slug_hash_bucket(slug: str, n: int) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    return int(digest, 16) % n


def list_date_folder(date: str):
    """Single API call to list files in date folder (non-recursive)."""
    prefix = f"batches/public-merged/{date}/"
    for attempt in range(3):
        try:
            items = list_repo_tree(
                repo_id=REPO,
                path=prefix,
                repo_type="dataset",
                token=os.getenv("HF_TOKEN"),
            )
            # items may include nested folders; we only want raw files
            files = [p.rfilename for p in items if not p.rfilename.endswith("/")]
            return files
        except Exception as exc:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return []


def build_manifest(date: str):
    """Create manifest.json with file list and shard map."""
    files = list_date_folder(date)
    manifest = {
        "date": date,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "shard_map": {},
    }
    for f in files:
        manifest["shard_map"][f] = slug_hash_bucket(f, TOTAL_SHARDS)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    return manifest


def cdn_url(path: str) -> str:
    """CDN URL that bypasses HF API auth/rate limits."""
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"


def stream_and_project(path: str):
    """
    Stream file and project to {prompt,response}.
    Handles common repo layouts:
      - {prompt, response}
      - {input, output}
      - {instruction, output}
      - single-column text (treated as prompt; response empty)
    """
    url = cdn_url(path)
    # Use streaming to avoid loading full file into memory
    try:
        ds = load_dataset("json", data_files={"train": url}, streaming=True)
    except Exception:
        # fallback: parquet or other; download raw bytes and parse minimally
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        # minimal heuristic: if parquet, rely on datasets non-streaming once per file
        if path.endswith(".parquet"):
            import pyarrow.parquet as pq
            import io
            table = pq.read_table(io.BytesIO(resp.content))
            df = table.to_pandas()
            for _, row in df.iterrows():
                d = dict(row)
                prompt, response = normalize_pair(d)
                if prompt is not None:
                    yield {"prompt": prompt, "response": response}
            return
        # try jsonl lines
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            prompt, response = normalize_pair(d)
            if prompt is not None:
                yield {"prompt": prompt, "response": response}
        return

    for row in ds["train"]:
        prompt, response = normalize_pair(row)
        if prompt is not None:
            yield {"prompt": prompt, "response": response}


def normalize_pair(d: dict):
    """Project dict to (prompt, response). Returns (None, None) if unusable."""
    if not isinstance(d, dict):
        return None, None

    # Preferred keys
    if "prompt" in d and "response" in d:
        return str(d["prompt"]), str(d["response"])
    if "input" in d and "output" in d:
        return str(d["input"]), str(d["output"])
    if "instruction" in d and "output" in d:
        return str(d["instruction"]), str(d["output"])

    # Fallback: single text field
    if "text" in d:
        return str(d["text"]), ""

    # Last resort: first/second column heuristic
    keys = [k for k in d.keys() if isinstance(k, str)]
    if len(keys) >= 2:
        return str(d[keys[0]]), str(d[keys[1]])
    return None, None


def process_shard(manifest):
    """Download and emit assigned files."""
    assigned = [
        f for f, sid in manifest["shard_map"].items() if sid == SHARD_ID
    ]
    if not assigned:
        print(f"Shard {SHARD_ID}: no files assigned.")
        return

    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out
