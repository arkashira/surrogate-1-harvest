# vanguard / backend

## 1. Diagnosis
- Training launcher performs authenticated HF API calls (`list_repo_tree`, `load_dataset`) on every run → burns quota and risks 429s.
- No CDN-bypass for dataset fetches during training; data loader uses authenticated `/api/` endpoints instead of public CDN URLs.
- No persisted file manifest per date folder → repeated API calls for the same file list across restarts/iterations.
- Lightning Studio reuse not enforced; training may recreate or fail on idle-stop without graceful restart.
- No fallback for 429/5xx during ingestion/training; failures bubble up instead of exponential backoff + resume.

## 2. Proposed change
- **Scope**: `/opt/axentx/vanguard/backend/train.py` (or equivalent launcher) + new `/opt/axentx/vanguard/backend/manifest.py` + `/opt/axentx/vanguard/backend/data.py`.
- **Goal**: Single authenticated `list_repo_tree` → save JSON manifest → Lightning training uses CDN-only fetches via public URLs with zero API calls during data load; add graceful studio reuse and 429 resilience.

## 3. Implementation

### Create `/opt/axentx/vanguard/backend/manifest.py`
```python
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree, HfApi
except ImportError:
    HfApi = None

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def build_manifest(repo_id: str, date_folder: str, out_path: str | None = None) -> Dict:
    """
    Single authenticated call to list files for one date folder.
    Returns manifest and saves JSON for reuse.
    """
    if HfApi is None:
        raise RuntimeError("huggingface_hub not installed")

    api = HfApi()
    # Non-recursive per folder to minimize pagination/cost; caller can recurse if needed.
    tree = list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = [{"path": f.path, "size": getattr(f, "size", None)} for f in tree if not f.type == "directory"]

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": _now_iso(),
        "files": files,
    }

    if out_path is None:
        slug = repo_id.replace("/", "_")
        out_path = MANIFEST_DIR / f"{slug}__{date_folder}.json"
    else:
        out_path = Path(out_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest

def load_manifest(repo_id: str, date_folder: str) -> Dict | None:
    slug = repo_id.replace("/", "_")
    p = MANIFEST_DIR / f"{slug}__{date_folder}.json"
    if not p.is_file():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def cdn_url(repo_id: str, file_path: str, revision: str = "main") -> str:
    """
    Public CDN URL — no Authorization header required.
    """
    return f"https://huggingface.co/datasets/{repo_id}/resolve/{revision}/{file_path}"
```

### Update `/opt/axentx/vanguard/backend/data.py` (create or modify)
```python
import io
import json
import time
import random
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import hf_hub_download

from .manifest import load_manifest, cdn_url

HF_RETRY_WAIT = 360  # seconds after 429
MAX_RETRIES = 5
BACKOFF_BASE = 2

def robust_get(url: str, headers: dict | None = None, timeout: int = 30) -> requests.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
            if resp.status_code == 429:
                wait = HF_RETRY_WAIT + random.uniform(0, 60)
                print(f"429 rate-limited, waiting {wait:.0f}s (attempt {attempt})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.HTTPError, requests.ConnectionError) as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_BASE ** attempt + random.uniform(0, 1)
            print(f"Retryable error {exc}, waiting {wait:.1f}s (attempt {attempt})")
            time.sleep(wait)
    raise RuntimeError("Unreachable")

def iter_cdn_parquet_files(repo_id: str, date_folder: str) -> Iterator[Dict[str, Any]]:
    """
    Uses persisted manifest and CDN URLs to stream parquet rows as dicts.
    No authenticated HF API calls during training.
    """
    manifest = load_manifest(repo_id, date_folder)
    if manifest is None:
        raise FileNotFoundError(f"No manifest for {repo_id}/{date_folder}. Run manifest.build_manifest first.")

    for f in manifest["files"]:
        path = f["path"]
        if not path.lower().endswith(".parquet"):
            continue
        url = cdn_url(repo_id, path, revision="main")
        resp = robust_get(url)
        # Stream into pyarrow without full download to disk
        with io.BytesIO() as bio:
            for chunk in resp.iter_content(chunk_size=8192):
                bio.write(chunk)
            bio.seek(0)
            try:
                table = pq.read_table(bio)
            except pa.ArrowInvalid:
                print(f"Skipping invalid parquet: {path}")
                continue

        # Project only required fields at parse time
        cols = set(table.column_names)
        prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)

        if not prompt_col or not response_col:
            # skip files that don't match expected schema
            continue

        df = table.select([prompt_col, response_col]).to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": str(row[prompt_col]), "response": str(row[response_col])}
```

### Update `/opt/axentx/vanguard/backend/train.py` (or launcher)
```python
import os
import sys
from pathlib import Path

try:
    import lightning as L
    from lightning.fabric.plugins import TorchMetrics
except ImportError:
    L = None

from .manifest import build_manifest, load_manifest
from .data import iter_cdn_parquet_files

HF_REPO = os.getenv("HF_REPO", "your-org/your-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-05-03")
MANIFEST_ONLY = os.getenv("MANIFEST_ONLY", "0") == "1"

def ensure_manifest():
    m = load_manifest(HF_REPO, DATE_FOLDER)
    if m is None:
        print("Building manifest (single authenticated call)...")
        m = build_manifest(HF_REPO, DATE_FOLDER)
        print(f"Manifest saved with {len(m['files'])} files.")
    return m

def train_step():
    # Example training loop using CDN-only data
    count = 0
    for item in iter_cdn_parquet_files(HF_REPO, DATE_FOLDER):
        # Replace with your surrogate-1 collate/train step
        # e.g., tokenizer, forward, backward
        count += 1
        if count % 1000 == 0:
            print(f"Processed {count} samples")
    print("Training epoch complete (CDN-only).")

def reuse_or_create_studio():
    if L is None:
       
