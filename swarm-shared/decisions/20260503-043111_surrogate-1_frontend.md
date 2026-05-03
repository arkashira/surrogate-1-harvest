# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/manifest.py`**  
   - Single API call (`list_repo_tree` recursive=False) to list date folders under `public-merged/`.  
   - For each date folder, list files (non-recursive) and write `manifest.json` containing `{rel_path, cdn_url, sha256}`.  
   - Embed this manifest in the training workflow so Lightning workers do **zero HF API calls** during data loading.

2. **Add `bin/worker.py`**  
   - Deterministic shard assignment via `hash(rel_path) % TOTAL_SHARDS == SHARD_ID`.  
   - Download assigned files via **CDN bypass** (`resolve/main/...`) — no auth header, separate rate limits.  
   - Project each row to `{prompt, response}` at parse time (avoids pyarrow `CastError` from mixed schemas).  
   - Deduplicate via existing `lib/dedup.py` md5 store.  
   - Write normalized JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.  
   - Commit via `huggingface_hub` (single commit per shard).

3. **Update `bin/dataset-enrich.sh`**  
   - Export `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`.  
   - Invoke `python3 bin/worker.py "$@"` with proper quoting.  
   - Fall back to logging-only mode if `HF_TOKEN` missing.

4. **Update `.github/workflows/ingest.yml`**  
   - Matrix `shard_id: [0..15]`.  
   - `timeout-minutes: 30`.  
   - `env.HF_TOKEN` secret present.  
   - Runs on `ubuntu-latest` (7 GB per shard).

5. **Add `requirements.txt` entries** (if missing):
   ```
   huggingface_hub>=0.22
   pyarrow>=14
   numpy
   requests
   ```

---

### Code Snippets

#### `bin/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for public-merged/ to avoid HF API calls during training.

Usage:
  HF_TOKEN=<token> python3 bin/manifest.py
"""

from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
from typing import List, Dict

import requests
from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_DIR = Path(__file__).parent.parent
HF_TOKEN = os.getenv("HF_TOKEN")
API = HfApi(token=HF_TOKEN) if HF_TOKEN else None
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

def list_date_folders() -> List[str]:
    """Single API call: non-recursive tree under public-merged/."""
    if not API:
        return []
    items = API.list_repo_tree(
        repo_id=REPO_ID,
        path="public-merged",
        recursive=False,
    )
    return [p.rstrip("/") for p in items if p and "/" in p]

def list_files_in_folder(folder: str) -> List[str]:
    """List files in a date folder (non-recursive)."""
    if not API:
        return []
    items = API.list_repo_tree(
        repo_id=REPO_ID,
        path=f"public-merged/{folder}",
        recursive=False,
    )
    return [p for p in items if p and not p.endswith("/")]

def build_manifest() -> Dict[str, List[Dict[str, str]]]:
    """Build manifest mapping date folders to files with CDN URLs."""
    manifest: Dict[str, List[Dict[str, str]]] = {}
    for folder in list_date_folders():
        files = list_files_in_folder(folder)
        manifest[folder] = [
            {
                "rel_path": f"public-merged/{folder}/{f}",
                "cdn_url": f"{CDN_ROOT}/public-merged/{folder}/{f}",
            }
            for f in files
        ]
    return manifest

def main() -> None:
    manifest = build_manifest()
    out_path = BASE_DIR / "manifest.json"
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path}")

if __name__ == "__main__":
    main()
```

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List

import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_DIR = Path(__file__).parent.parent
DEDUP_DB = BASE_DIR / "lib" / "dedup.py"  # existing module
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
API = HfApi(token=HF_TOKEN) if HF_TOKEN else None

# CDN bypass: no auth header, separate rate limits
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

def load_manifest() -> Dict[str, List[Dict[str, str]]]:
    manifest_path = BASE_DIR / "manifest.json"
    if not manifest_path.exists():
        return {}
    with open(manifest_path) as f:
        return json.load(f)

def assign_to_shard(key: str, shard_id: int, total: int) -> bool:
    """Deterministic shard assignment by key hash."""
    digest = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return (digest % total) == shard_id

def download_via_cdn(path: str, local_path: Path) -> None:
    """Download dataset file via CDN (no auth)."""
    url = f"{CDN_ROOT}/{path}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def project_to_pair(batch: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schema to {prompt, response} only.
    Best-effort field mapping; missing values become empty strings.
    """
    prompt = ""
    response = ""

    # Common field names seen across schemas
    for pfield in ("prompt", "instruction", "input", "question", "text"):
        if pfield in batch and batch[pfield] is not None:
            prompt = str(batch[pfield]).strip()
            break

    for rfield in ("response", "output", "answer", "completion"):
        if rfield in batch and batch[rfield] is not None:
            response = str(batch[rfield]).strip()
            break

    # Fallback: if only one text-like column exists, split by separator
    if not prompt and not response:
        for k, v in batch.items():
            if isinstance(v, str) and v.strip():
                parts = v.split("\n\n", 1)
                if len(parts) == 2:
                    prompt, response = parts[0].strip(), parts[1].strip()
                else:
                    prompt = v.strip()
                break

    return {"prompt": prompt, "response": response}

def hash_pair(pair: Dict[str, str]) -> str:
    src = f"{pair['prompt']}\n\n{pair['response']}"
    return hashlib.md5(src.encode()).hexdigest()

def is_duplicate(md5_hex: str) -> bool:
    # Import existing dedup module if available

