# vanguard / backend

## Final synthesized implementation (best of both proposals)

**Diagnosis (resolved)**
- Frontend re-enumerates HF API on every page load → burn quota, risk 429s.  
- Backend uses authenticated paths instead of public CDN → avoidable rate-limit/auth overhead.  
- No single source-of-truth for available training shards; ingestion/training re-list independently.  
- No lightweight endpoint to serve manifest to frontend.  
- Ingestion can land mixed-schema parquet in `enriched/` instead of projecting to `{prompt, response}`.

**Core design choices (favor correctness + actionability)**
- Use **one** `list_repo_tree(recursive=False)` call per `(repo, dateFolder)` and cache result.  
- Cache on disk (JSON) with 1-hour TTL (long enough to avoid churn, short enough to pick up new shards).  
- Return public CDN base so frontend/training can fetch without auth.  
- Add surrogate-1 schema guard at ingestion time (project to `{prompt: str, response: str}` and drop extras).  
- Keep endpoint simple, idempotent, and restart-safe (disk cache + optional in-memory lock for concurrent requests).

---

### 1. Create directories
```bash
mkdir -p /opt/axentx/vanguard/backend/api /opt/axentx/vanguard/data/manifests
```

---

### 2. `/opt/axentx/vanguard/backend/manifest.py`
```python
import json
import os
import time
import fcntl
from pathlib import Path
from typing import Dict, Any, List

import huggingface_hub

MANIFEST_DIR = Path(__file__).parent.parent.parent / "data" / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

def _cache_path(repo: str, date_folder: str) -> Path:
    safe = f"{repo.replace('/', '__')}__{date_folder.replace('/', '_')}.json"
    return MANIFEST_DIR / safe

def _load_cached(cache_file: Path, ttl_seconds: int) -> Dict[str, Any] | None:
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data.get("_cached_at", 0) < ttl_seconds:
            return data
        # stale; ignore
    except Exception:
        cache_file.unlink(missing_ok=True)
    return None

def _save_cached(cache_file: Path, payload: Dict[str, Any]) -> None:
    # atomic write + best-effort cross-process lock to dedupe concurrent builds
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    try:
        with cache_file.open("wb"):
            fcntl.lockf(cache_file, fcntl.LOCK_EX)
        tmp.replace(cache_file)
    finally:
        tmp.unlink(missing_ok=True)

def get_manifest(repo: str, date_folder: str, ttl_seconds: int = 3600) -> Dict[str, Any]:
    """
    Return cached manifest if fresh, else build via a single list_repo_tree call.
    Uses public CDN base for file fetches (bypasses HF API auth during training/load).
    """
    cache_file = _cache_path(repo, date_folder)
    cached = _load_cached(cache_file, ttl_seconds)
    if cached is not None:
        return cached

    # Single API call: non-recursive, top-level only
    items = huggingface_hub.list_repo_tree(
        repo=repo,
        path=date_folder,
        recursive=False,
        repo_type="dataset",
    )

    files = [
        item["path"].split("/")[-1]
        for item in items
        if item.get("type") == "file" and item["path"].endswith(".parquet")
    ]

    result = {
        "repo": repo,
        "date_folder": date_folder,
        "files": sorted(files),
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}",
        "_cached_at": time.time(),
    }
    _save_cached(cache_file, result)
    return result


def surrogate1_projection(raw_record: Dict[str, Any]) -> Dict[str, str]:
    """
    Enforce surrogate-1 {prompt, response} schema.
    Drop extra columns; coerce to str; never persist source/ts into enriched parquet.
    """
    prompt = str(raw_record.get("prompt", raw_record.get("input", "")))
    response = str(raw_record.get("response", raw_record.get("output", "")))
    return {"prompt": prompt, "response": response}
```

---

### 3. `/opt/axentx/vanguard/backend/api/manifest.py`
```python
from fastapi import APIRouter, HTTPException

from ..manifest import get_manifest

router = APIRouter(prefix="/api/manifest", tags=["manifest"])

@router.get("/{repo}/{date_folder}")
def manifest_endpoint(repo: str, date_folder: str):
    try:
        return get_manifest(repo=repo, date_folder=date_folder)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

---

### 4. Wire into FastAPI app
In `/opt/axentx/vanguard/backend/main.py` (or wherever `app = FastAPI(...)` lives):
```python
from backend.api.manifest import router as manifest_router

app.include_router(manifest_router)
```

---

### 5. Guard ingestion (apply where you write `enriched/`)
```python
from backend.manifest import surrogate1_projection

# Before writing parquet to enriched/:
records = [surrogate1_projection(r) for r in raw_records]
# write records -> parquet (only prompt,response)
```

---

### 6. Verification
1. Start/reload backend server.
2. Request manifest:
   ```bash
   curl "http://localhost:8000/api/manifest/mirror-org/mirror-merged/2026-04-29"
   ```
   Expect JSON with `files` list and `cdn_base`.
3. Confirm cache file:
   ```bash
   ls -la /opt/axentx/vanguard/data/manifests/
   cat /opt/axentx/vanguard/data/manifests/mirror-org__mirror-merged_2026-04-29.json
   ```
4. Re-request within 1h → `_cached_at` unchanged; no new HF API calls.
5. Confirm `enriched/*.parquet` schema is exactly `{prompt: string, response: string}` (e.g., via `parquet-tools schema` or pandas `df.columns.tolist()`).
