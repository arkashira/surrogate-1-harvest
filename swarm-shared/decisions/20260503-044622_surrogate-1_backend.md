# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data load and prevents mixed-schema `CastError`s.

### Changes

1. **Add `bin/worker.py`** — single-file worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` from the matrix.
   - Uses **one HF API call per date folder** via `list_repo_tree(..., recursive=False)` to list files non-recursively, then **caches the full manifest to `manifest.json`** so Lightning training does **zero API calls** during data loading.
   - Filters files deterministically by `hash(slug) % TOTAL_SHARDS == SHARD_ID`.
   - Downloads selected files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no auth header.
   - Projects each file to `{prompt, response}` at parse time (avoids mixed-schema `CastError`).
   - Deduplicates via `lib/dedup.py` (central md5 store).
   - Writes output as `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **Update `bin/dataset-enrich.sh`** — thin wrapper that:
   - Sets `#!/usr/bin/env bash`, `set -euo pipefail`.
   - Exports `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`.
   - Invokes `python3 bin/worker.py "$@"`.

3. **Update `.github/workflows/ingest.yml`** — ensure:
   - Matrix uses `shard: [0..15]`.
   - Runs with `bash bin/dataset-enrich.sh`.
   - No recursive `list_repo_files`; rely on worker’s non-recursive tree listing.

4. **Add `requirements.txt` entries** (if missing): `requests`, `tqdm`, `python-slugify` (optional), `pyarrow` (for Parquet support).

---

### Code Snippets

#### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py

Environment:
  HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
  HF_REPO          - default: datasets/axentx/surrogate-1-training-pairs
  DEDUP_DB_PATH    - path to central md5 dedup store (default: lib/dedup.py)
"""
import os
import json
import hashlib
import datetime
import time
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN is required")

API = HfApi(token=HF_TOKEN)

# CDN base (no auth, bypasses /api/ rate limits)
CDN_BASE = f"https://huggingface.co/{HF_REPO}/resolve/main"

# Deterministic sharding
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
assert 0 <= SHARD_ID < TOTAL_SHARDS

# Output root
OUT_ROOT = Path("batches/public-merged")
OUT_ROOT.mkdir(parents=True, exist_ok=True)

# Dedup store
DEDUP_DB_PATH = Path(os.getenv("DEDUP_DB_PATH", "lib/dedup.py"))
if DEDUP_DB_PATH.exists():
    # Import the module dynamically to reuse existing dedup logic
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", DEDUP_DB_PATH)
    dedup_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dedup_mod)
    seen = dedup_mod.SeenHashStore()
else:
    # Minimal fallback
    class SeenHashStore:
        def __init__(self):
            self._hashes = set()
        def exists(self, h: str) -> bool:
            return h in self._hashes
        def add(self, h: str) -> None:
            self._hashes.add(h)
    seen = SeenHashStore()

def hf_tree_list(path: str = "") -> List[Dict[str, Any]]:
    """Non-recursive tree listing to avoid paginated list_repo_files."""
    items = API.list_repo_tree(repo_id=HF_REPO, path=path, recursive=False)
    return [i for i in items if i.get("type") in {"file", "blob"}]

def build_manifest() -> List[Dict[str, str]]:
    """
    Build manifest of candidate files.
    Returns list of dicts with keys: path, date (if detectable).
    """
    manifest = []
    # List top-level date folders (YYYY-MM-DD) non-recursively
    top_items = hf_tree_list("")
    date_folders = [
        item for item in top_items
        if item.get("type") == "tree" and item.get("path", "").count("-") == 2
    ]
    for df in date_folders:
        date = df["path"]
        files = hf_tree_list(date)
        for f in files:
            if f.get("type") != "file":
                continue
            fp = f"{date}/{f['path']}"
            manifest.append({"path": fp, "date": date})
    return manifest

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % TOTAL_SHARDS == SHARD_ID

def cdn_download(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_to_pair(raw: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous files to {prompt,response} at parse time.
    Supports:
      - JSONL with {prompt, response}
      - JSONL with {input, output}
      - Parquet (via pyarrow) -> project only prompt/response columns
    """
    import io
    # Try JSONL first
    try:
        text = raw.decode("utf-8")
        pairs = []
        for line in text.strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if "prompt" in obj and "response" in obj:
                pairs.append({"prompt": str(obj["prompt"]), "response": str(obj["response"])})
            elif "input" in obj and "output" in obj:
                pairs.append({"prompt": str(obj["input"]), "response": str(obj["output"])})
            else:
                # Best-effort: use first two string fields
                keys = [k for k in obj if isinstance(obj[k], str)]
                if len(keys) >= 2:
                    pairs.append({"prompt": str(obj[keys[0]]), "response": str(obj[keys[1]])})
        if pairs:
            return pairs
    except Exception:
        pass

    # Try parquet
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(io.BytesIO(raw))
        cols = table.column_names
        prompt_col = next((c for c in ["prompt", "input", "question"] if c in cols), None)
        response_col = next((c for c in ["response", "output", "answer"] if c in cols), None)
        if prompt_col and response_col:
            df = table.select([prompt_col, response_col]).to_pandas()
            df.columns = ["prompt", "response"]
            df = df.dropna(subset=["prompt", "response"])
            return df.to_dict(orient="records")
    except Exception:
        pass

    # Fallback: return empty to skip
    return []

def run() -> None:
    manifest = build_manifest()
    print(f"Found {len(manifest)} candidate files; shard {SHARD_ID}/{TOTAL_SHARDS}")

    # Deterministic shard selection by file path slug
    selected = []
    for item
