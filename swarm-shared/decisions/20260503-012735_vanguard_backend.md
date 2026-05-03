# vanguard / backend

## 1. Diagnosis
- Frontend still triggers authenticated `list_repo_tree` via backend proxy on training page load, burning HF API quota (1000/5min) and causing 429s.
- No persisted file-list cache: each page load re-enumerates repo tree instead of using a saved JSON for a given `(repo, dateFolder)`.
- Backend routes still use HF SDK/`/api/` paths for file metadata instead of public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`).
- No fallback when HF API is rate-limited: training script fails instead of using CDN-only file list.
- Missing idempotent endpoint to pre-compute and store file list once per `(repo, dateFolder)` for Lightning training jobs.

## 2. Proposed change
- Add `/opt/axentx/vanguard/backend/api/v1/cache_filelist.py` (FastAPI route) + `/opt/axentx/vanguard/backend/services/hf_cdn.py` (service layer).
- Modify `/opt/axentx/vanguard/backend/api/v1/training.py` to use CDN URLs and cached file lists.
- Add JSON cache under `/opt/axentx/vanguard/backend/data/filelists/{repo_slug}/{dateFolder}.json`.

## 3. Implementation

```bash
# Ensure directories exist
mkdir -p /opt/axentx/vanguard/backend/services
mkdir -p /opt/axentx/vanguard/backend/data/filelists
```

### backend/services/hf_cdn.py
```python
# /opt/axentx/vanguard/backend/services/hf_cdn.py
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict

import httpx
from huggingface_hub import HfApi, list_repo_tree

HF_API = HfApi()
CDN_BASE = "https://huggingface.co/datasets"
CACHE_ROOT = Path(__file__).parent.parent.parent / "data" / "filelists"

def _cache_path(repo: str, date_folder: str) -> Path:
    safe_repo = repo.replace("/", "_")
    return CACHE_ROOT / safe_repo / f"{date_folder}.json"

def list_and_cache_files(repo: str, date_folder: str, token: str = None) -> List[Dict]:
    """
    Single authenticated API call to list files for one date folder,
    then cache to JSON for CDN-only training.
    """
    cache_p = _cache_path(repo, date_folder)
    cache_p.parent.mkdir(parents=True, exist_ok=True)

    # If fresh cache exists (<12h), reuse it to avoid extra API calls
    if cache_p.exists() and (datetime.utcnow().timestamp() - cache_p.stat().st_mtime) < 43200:
        return json.loads(cache_p.read_text())

    # One paginated call: non-recursive inside the date folder
    tree = list_repo_tree(
        repo_id=repo,
        path=date_folder,
        recursive=False,
        token=token,
    )

    files = []
    for entry in tree:
        if entry.type == "file":
            files.append({
                "path": f"{date_folder}/{entry.path.split('/')[-1]}",
                "cdn_url": f"{CDN_BASE}/{repo}/resolve/main/{date_folder}/{entry.path.split('/')[-1]}",
                "size": getattr(entry, "size", None),
            })

    cache_p.write_text(json.dumps(files, indent=2))
    return files

def cdn_download_urls(file_list: List[Dict]) -> List[str]:
    return [f["cdn_url"] for f in file_list]
```

### backend/api/v1/cache_filelist.py
```python
# /opt/axentx/vanguard/backend/api/v1/cache_filelist.py
from fastapi import APIRouter, HTTPException, Depends
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from ..deps import get_hf_token
from ...services.hf_cdn import list_and_cache_files

router = APIRouter(prefix="/cache", tags=["cache"])

@router.post("/filelist")
def cache_filelist(repo: str, date_folder: str, token: str = Depends(get_hf_token)):
    """
    Pre-compute and persist file list for a (repo, dateFolder).
    Call once (e.g., from cron or admin UI) to avoid repeated list_repo_tree.
    """
    try:
        files = list_and_cache_files(repo, date_folder, token=token)
        return {"repo": repo, "date_folder": date_folder, "files": len(files), "cached": True}
    except Exception as exc:
        if "429" in str(exc):
            raise HTTPException(status_code=HTTP_429_TOO_MANY_REQUESTS, detail="HF API rate limit")
        raise HTTPException(status_code=500, detail=str(exc))
```

### backend/api/v1/training.py (patch)
```python
# Add near top
from ...services.hf_cdn import list_and_cache_files, cdn_download_urls

# Replace any route that previously called list_repo_tree or /api/ proxy with:
@router.get("/training/filelist")
def training_filelist(repo: str, date_folder: str, use_cache: bool = True, token: str = Depends(get_hf_token)):
    """
    Returns CDN URLs for training data loader.
    Uses cached JSON when possible (zero HF API calls during training).
    """
    try:
        files = list_and_cache_files(repo, date_folder, token=token) if use_cache else []
        if not files:
            # fallback: one-time list if cache missing
            files = list_and_cache_files(repo, date_folder, token=token)
        return {"urls": cdn_download_urls(files), "count": len(files)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

### Update Lightning training script stub (example)
```python
# In Lightning train.py (run on Studio), read embedded file list or hit CDN-only:
import json
from pathlib import Path

def load_filelist(repo: str, date_folder: str):
    p = Path("data/filelists") / repo.replace("/", "_") / f"{date_folder}.json"
    if p.exists():
        return json.loads(p.read_text())
    raise FileNotFoundError("Embed filelist JSON in training job to avoid HF API during train.")
```

## 4. Verification
1. Start backend server and call:
   ```bash
   curl -X POST "http://localhost:8000/api/v1/cache/filelist?repo=myorg/vanguard-data&date_folder=batches/2026-05-03"
   ```
   Expect `{"repo":..., "files": N, "cached": true}` and file created at `backend/data/filelists/myorg_vanguard-data/2026-05-03.json`.

2. Call training filelist endpoint:
   ```bash
   curl "http://localhost:8000/api/v1/training/filelist?repo=myorg/vanguard-data&date_folder=batches/2026-05-03"
   ```
   Expect `{"urls": ["https://huggingface.co/datasets/.../resolve/main/..."], "count": N}` with no authenticated `/api/` calls in server logs.

3. Confirm CDN URLs are reachable without Authorization header:
   ```bash
   curl -I "https://huggingface.co/datasets/myorg/vanguard-data/resolve/main/batches/2026-05-03/some.parquet"
   ```
   Expect `200 OK` or `404` (not `401`).

4. In Lightning Studio, run a minimal training job that loads the JSON file list and streams via CDN URLs; verify zero HF API calls during data loading (no 429s, no `huggingface_hub` client logs).
