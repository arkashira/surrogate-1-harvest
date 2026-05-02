# vanguard / backend

## 1. Diagnosis

- HF API quota is burned by repeated `list_repo_tree` / `list_repo_files` calls from backend endpoints (training triggers, dataset indexing) instead of using a persisted manifest + CDN-only fetches.
- No durable file manifest cache: every training or dataset request re-enumerates repo contents, causing 429s and slow responses.
- Dataset ingestion likely uses `load_dataset(streaming=True)` on heterogeneous repos, risking `pyarrow` schema errors and redundant API calls.
- Training jobs re-list repos on every run instead of embedding a static file list, wasting quota and making Lightning runs fragile.
- No CDN bypass strategy: backend still routes dataset reads through authenticated HF API instead of using public `resolve/main/` URLs.

## 2. Proposed change

Add a backend manifest service that:
- Persists repo file listings to `vanguard/data/manifests/{repo_slug}/{date}/files.json`
- Exposes endpoints to refresh/read manifest (single API call per date folder)
- Embeds the manifest into training payloads so Lightning training uses CDN-only fetches
- Adds a dataset loader that uses `hf_hub_download` per file (CDN) and projects to `{prompt, response}` only

Scope:
- Create `vanguard/backend/services/manifest_service.py`
- Create `vanguard/backend/services/dataset_loader.py`
- Add util `vanguard/backend/utils/hf_cdn.py`
- Update training launcher to accept/embed file list
- Add FastAPI routes: `POST /manifest/refresh`, `GET /manifest/{repo_slug}/{date}`

## 3. Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/backend/{services,utils,routes}
mkdir -p /opt/axentx/vanguard/data/manifests
```

### `vanguard/backend/utils/hf_cdn.py`
```python
import os
import json
import time
import httpx
from pathlib import Path
from typing import List, Dict, Optional
from huggingface_hub import HfApi, list_repo_tree

HF_API = HfApi()
CDN_ROOT = "https://huggingface.co/datasets"

def list_repo_files_safe(
    repo_id: str,
    path: str = "",
    recursive: bool = False,
    retries: int = 3,
    backoff: int = 360
) -> List[str]:
    """Single API call to list files in one folder (non-recursive)."""
    for attempt in range(retries):
        try:
            items = list_repo_tree(
                repo_id=repo_id,
                path=path,
                recursive=recursive,
                token=os.getenv("HF_TOKEN")
            )
            # items are dict-like; extract path
            return [item["path"] for item in items if item.get("type") == "file"]
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                time.sleep(backoff)
                continue
            raise
    return []


def cdn_download_url(repo_id: str, file_path: str) -> str:
    """Public CDN URL — no auth required, higher rate limits."""
    return f"{CDN_ROOT}/{repo_id}/resolve/main/{file_path}"


def save_manifest(repo_id: str, date_folder: str, files: List[str], base_dir: str = "data/manifests"):
    out_dir = Path(base_dir) / repo_id.replace("/", "_") / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "files.json"
    manifest_path.write_text(json.dumps({"repo_id": repo_id, "date": date_folder, "files": files}, indent=2))
    return str(manifest_path)


def load_manifest(repo_id: str, date_folder: str, base_dir: str = "data/manifests") -> Optional[List[str]]:
    manifest_path = Path(base_dir) / repo_id.replace("/", "_") / date_folder / "files.json"
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text())
        return data.get("files", [])
    return None
```

### `vanguard/backend/services/dataset_loader.py`
```python
import json
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
from typing import List, Dict, Iterator
from huggingface_hub import hf_hub_download
from vanguard.backend.utils.hf_cdn import cdn_download_url
import httpx
import os

def stream_parquet_from_cdn(repo_id: str, file_path: str, columns: List[str] = None) -> Iterator[Dict]:
    """Download single parquet via CDN and stream rows as dicts."""
    url = cdn_download_url(repo_id, file_path)
    local_path = hf_hub_download(repo_id=repo_id, filename=file_path, token=os.getenv("HF_TOKEN"))
    table = pq.read_table(local_path, columns=columns)
    for batch in table.to_batches(max_chunksize=1024):
        for row in batch.to_pylist():
            yield row


def build_training_rows(repo_id: str, file_paths: List[str], prompt_key: str = "prompt", response_key: str = "response") -> List[Dict]:
    """
    Download each file individually and project to {prompt, response}.
    Avoids load_dataset(streaming=True) on heterogeneous schemas.
    """
    rows = []
    for fp in file_paths:
        try:
            for row in stream_parquet_from_cdn(repo_id, fp, columns=[prompt_key, response_key]):
                rows.append({
                    "prompt": row.get(prompt_key, ""),
                    "response": row.get(response_key, "")
                })
        except Exception as e:
            # Skip malformed files; log in production
            continue
    return rows
```

### `vanguard/backend/services/manifest_service.py`
```python
import os
from typing import List
from vanguard.backend.utils.hf_cdn import list_repo_files_safe, save_manifest, load_manifest

def refresh_manifest(repo_id: str, date_folder: str, path: str = "") -> List[str]:
    """
    Single API call to list files in date_folder (non-recursive).
    Persists to data/manifests/{repo_slug}/{date}/files.json
    """
    # If repo_id is dataset repo, path can be date_folder inside it
    full_path = os.path.join(path, date_folder).strip("/")
    files = list_repo_files_safe(repo_id=repo_id, path=full_path, recursive=False)
    # Filter to parquet for training
    parquet_files = [f for f in files if f.endswith(".parquet")]
    save_manifest(repo_id, date_folder, parquet_files)
    return parquet_files


def get_manifest(repo_id: str, date_folder: str) -> List[str]:
    cached = load_manifest(repo_id, date_folder)
    if cached is not None:
        return cached
    # Fallback: refresh once
    return refresh_manifest(repo_id, date_folder)
```

### `vanguard/backend/routes/manifest_routes.py` (FastAPI)
```python
from fastapi import APIRouter, HTTPException
from vanguard.backend.services.manifest_service import refresh_manifest, get_manifest

router = APIRouter(prefix="/manifest", tags=["manifest"])

@router.post("/refresh/{repo_id}/{date_folder}")
def refresh(repo_id: str, date_folder: str):
    try:
        files = refresh_manifest(repo_id, date_folder)
        return {"repo_id": repo_id, "date": date_folder, "files": files, "count": len(files)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{repo_id}/{date_folder}")
def read(repo_id: str, date_folder: str):
    try:
        files = get_manifest(repo_id, date_folder)
        return {"repo_id": repo_id, "date": date_folder, "files": files, "count": len(files)}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
```

### Wire into app (example snippet)
In your main FastAPI app file:
```python
from vanguard.backend.routes.manifest_routes import router as manifest_router
app.include_router(manifest_router)
```

### Training launcher usage (example)
```python
from vanguard.backend.services.manifest_service import get_manifest
from vanguard.backend.services.dataset_loader import build_training_rows
