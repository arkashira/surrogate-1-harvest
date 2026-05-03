# vanguard / backend

Below is the **single, consolidated implementation** that keeps the strongest, most actionable parts from both candidates, removes duplication, and resolves contradictions in favor of **correctness + concrete actionability**.

---

## 1. Diagnosis (resolved)
- **Problem**: No persisted `(repo, dateFolder)` manifest → every request triggers authenticated `list_repo_tree`/HF API calls, burning quota and risking 429s.
- **Problem**: Data fetches use authenticated `/api/` paths instead of public CDN URLs, causing avoidable rate-limit pressure.
- **Problem**: No caching for file lists → repeated page loads/training starts re-query HF for identical metadata.
- **Problem**: No fallback when HF API is rate-limited; training/data loads fail instead of degrading gracefully.
- **Add (from C2)**: Thundering herd on concurrent training starts; missing request coalescing/deduplication.

---

## 2. Design decisions (resolved)
- **Manifest persistence**: JSON per `(repo, dateFolder)` saved under `backend/data/manifests/{repo_slug}/{dateFolder}.json`. TTL 24h (configurable).
- **CDN-first downloads**: All dataset file fetches use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).
- **Cache strategy**:
  - Disk-backed manifest for durability across restarts.
  - In-memory TTL cache for `list_repo_tree` results keyed by `(repo, path, recursive=False)` to dedupe concurrent requests.
- **Coalescing**: In-memory lock per key so concurrent callers block on one upstream HF call instead of stampeding.
- **Lightning Studio reuse**: List running studios and restart only if stopped (kept minimal and optional).
- **No mixing of auth for data**: Backend uses auth only for manifest build; everything else uses CDN.

---

## 3. File layout (single source of truth)
```
/opt/axentx/vanguard/
├── backend/
│   ├── config.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── hf_service.py
│   ├── api/
│   │   └── routers/
│   │       └── data.py
│   └── data/
│       └── manifests/
```

---

## 4. Implementation

### `/opt/axentx/vanguard/backend/config.py`
```python
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

HF_DATASETS_REPO = os.getenv("HF_DATASETS_REPO", "your-org/your-dataset")
MANIFEST_DIR = BASE_DIR / "data" / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

HF_CDN_BASE = "https://huggingface.co/datasets"
HF_API_BASE = "https://huggingface.co/api"

# Cache / TTL
MANIFEST_TTL_SECONDS = int(os.getenv("MANIFEST_TTL_SECONDS", 86400))  # 24h
HF_RATE_LIMIT_RETRY_SECONDS = int(os.getenv("HF_RATE_LIMIT_RETRY_SECONDS", 360))
HF_LIST_REPO_TREE_TIMEOUT = int(os.getenv("HF_LIST_REPO_TREE_TIMEOUT", 30))
```

---

### `/opt/axentx/vanguard/backend/services/hf_service.py`
```python
import json
import time
import hashlib
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download

from ..config import (
    MANIFEST_DIR,
    MANIFEST_TTL_SECONDS,
    HF_RATE_LIMIT_RETRY_SECONDS,
    HF_LIST_REPO_TREE_TIMEOUT,
    HF_CDN_BASE,
)

logger = logging.getLogger(__name__)

HF_API = HfApi()

# In-memory TTL cache + coalescing locks
_TTL_CACHE: dict = {}
_TTL_LOCKS: dict = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("||".join(str(p) for p in parts).encode()).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_cache_get(key: str):
    with _CACHE_LOCK:
        entry = _TTL_CACHE.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and _now_utc() > expires_at:
            del _TTL_CACHE[key]
            return None
        return value


def _ttl_cache_set(key: str, value, ttl_seconds: Optional[int] = None):
    expires_at = None
    if ttl_seconds is not None:
        expires_at = _now_utc().timestamp() + ttl_seconds
        expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    with _CACHE_LOCK:
        _TTL_CACHE[key] = (value, expires_at)


def _lock_for_key(key: str):
    with _CACHE_LOCK:
        if key not in _TTL_LOCKS:
            _TTL_LOCKS[key] = threading.Lock()
        return _TTL_LOCKS[key]


def _manifest_path(repo_slug: str, date_folder: str) -> Path:
    safe = repo_slug.replace("/", "_")
    return MANIFEST_DIR / safe / f"{date_folder}.json"


def _is_manifest_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age = (_now_utc() - mtime).total_seconds()
        return 0 <= age < MANIFEST_TTL_SECONDS
    except Exception:
        return False


def get_or_build_manifest(
    repo_slug: str,
    date_folder: str,
    token: Optional[str] = None,
    *,
    use_cache: bool = True,
) -> List[str]:
    """
    Returns repo-relative file paths for given repo/date.
    Strategy:
      1) Try fresh on-disk manifest.
      2) Try in-memory TTL cache (dedupes concurrent calls).
      3) Build via single list_repo_tree (coalesced), persist, and cache.
    """
    manifest = _manifest_path(repo_slug, date_folder)

    # 1) Disk (fast, survives restart)
    if use_cache and _is_manifest_fresh(manifest):
        try:
            with open(manifest) as f:
                paths = json.load(f)
            if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                logger.debug("Loaded manifest from disk: %s", manifest)
                return paths
        except Exception as e:
            logger.warning("Failed to read manifest %s: %s", manifest, e)

    cache_key = _cache_key("tree", repo_slug, date_folder, "recursive=False")

    # 2) In-memory TTL cache (dedupes within TTL)
    if use_cache:
        cached = _ttl_cache_get(cache_key)
        if cached is not None:
            logger.debug("Loaded manifest from memory cache: %s", cache_key)
            return cached

    # 3) Build (coalesced)
    lock = _lock_for_key(cache_key)
    with lock:
        # Double-check inside lock
        if use_cache:
            cached = _ttl_cache_get(cache_key)
            if cached is not None:
                return cached
        if manifest.exists() and _is_manifest_fresh(manifest):
            try:
                with open(manifest) as f:
                    paths = json.load(f)
                _ttl_cache_set(cache_key, paths, MANIFEST_TTL_SECONDS)
                return paths
            except Exception:
                pass

        logger.info("Building manifest for %s/%s", repo_slug, date_folder)
        try:
            tree = HF_API.list_repo_tree(
                repo_id=repo_slug,
                path=date_folder,
                repo_type="dataset",
                token=token,
                recursive=False,
                timeout=HF_LIST_REPO_TREE_TIMEOUT,
            )
        except Exception as e:
            # If manifest exists (even stale), prefer degraded mode over hard failure
            if manifest.exists():
                logger.warning("HF API failed, using stale manifest: %s", e)
                try:
                    with open(manifest
