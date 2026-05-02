# vanguard / backend

## Final Synthesized Implementation

**Core diagnosis (unified):**  
- No persisted file manifest → repeated HF tree/list calls → 429 (1000 req/5 min).  
- Lightning Studio recreated each run → quota burn + cold-start loss.  
- Training loads via HF API instead of CDN → redundant auth/rate pressure.  
- No deterministic repo selection for commits → 128 writes/hr/repo limit easily hit.  
- Missing snapshot determinism → training non-reproducible when repo changes mid-run.

**Chosen strategy (merged + hardened):**  
- Persist dated manifest per repo; TTL-based refresh; never cache 429.  
- Reuse Studio by name; start if stopped; fail fast if unavailable.  
- Embed manifest + CDN-only URLs into training snapshot; workers never call HF API for data.  
- Deterministic repo selection via hash for commit spreading.  
- Add lockfile for concurrent manifest writes; small retry/backoff for 429/5xx; explicit revision pinning.

---

### Directory layout
```
/opt/axentx/vanguard/
├── backend/
│   ├── __init__.py
│   ├── manifest.py
│   ├── studio.py
│   ├── train.py
│   └── utils.py
└── manifests/
    └── <repo_slug_safe>/<date>.json
```

---

### vanguard/backend/__init__.py
```python
# package marker
```

---

### vanguard/backend/utils.py
```python
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

def retries(
    n: int,
    backoff: float = 1.0,
    max_backoff: float = 30.0,
    should_retry: Callable[[Exception], bool] = lambda e: True,
) -> Callable[[Callable[[], T]], T]:
    def deco(fn: Callable[[], T]) -> T:
        delay = backoff
        last_exc: Exception | None = None
        for _ in range(n):
            try:
                return fn()
            except Exception as e:
                last_exc = e
                if not should_retry(e):
                    raise
                time.sleep(delay)
                delay = min(delay * 2, max_backoff)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unreachable")
    return deco


def repo_safe_slug(repo_slug: str) -> str:
    return repo_slug.replace("/", "_")
```

---

### vanguard/backend/manifest.py
```python
from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from vanguard.backend.utils import retries, repo_safe_slug

MANIFESTS_ROOT = Path(__file__).parent.parent.parent / "manifests"
HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"


def _cache_path(repo_slug: str, date_str: str) -> Path:
    return MANIFESTS_ROOT / repo_safe_slug(repo_slug) / f"{date_str}.json"


def _is_fresh(payload: Dict, ttl_seconds: int) -> bool:
    cached_at = payload.get("_cached_at")
    if not cached_at:
        return False
    try:
        ts = datetime.fromisoformat(cached_at)
        return (datetime.now(timezone.utc) - ts).total_seconds() < ttl_seconds
    except Exception:
        return False


@retries(
    n=3,
    backoff=2.0,
    should_retry=lambda e: getattr(e, "response", None) is not None and e.response.status_code in (429, 500, 502, 503, 504),
)
def list_repo_files_cached(
    repo_slug: str,
    folder: str = "",
    token: Optional[str] = None,
    ttl_seconds: int = 3600,
    revision: str = "main",
) -> List[str]:
    """
    Return deterministic file list for repo+folder+revision.
    Uses local cache to avoid HF API 429. Never caches 429.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_file = _cache_path(repo_slug, date_str)

    if cache_file.exists():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            meta = payload.get("_meta", {})
            if (
                meta.get("repo") == repo_slug
                and meta.get("folder") == folder
                and meta.get("revision") == revision
                and _is_fresh(payload, ttl_seconds)
            ):
                return payload["files"]
        except Exception:
            pass  # refresh

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _fetch_page(path: str, cursor: Optional[str] = None) -> Dict:
        url = f"{HF_API_BASE}/datasets/{repo_slug}/tree"
        params = {"path": path, "recursive": False, "revision": revision}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            # Attach response for retry decorator; do not cache
            e = RuntimeError("HF API 429 rate limit")
            e.response = resp
            raise e
        resp.raise_for_status()
        return resp.json()

    files: List[str] = []
    stack = [folder] if folder else [""]
    while stack:
        current = stack.pop()
        page = _fetch_page(current)
        if not isinstance(page, list):
            continue
        for item in page:
            if not isinstance(item, dict):
                continue
            typ = item.get("type")
            p = item.get("path")
            if not p:
                continue
            if typ == "file":
                files.append(p)
            elif typ == "folder":
                stack.append(p)

    payload = {
        "_cached_at": datetime.now(timezone.utc).isoformat(),
        "_meta": {
            "repo": repo_slug,
            "folder": folder,
            "revision": revision,
        },
        "files": sorted(set(files)),
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # Best-effort atomic write to avoid partial/corrupt cache under concurrency
    tmp = cache_file.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(cache_file)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    return payload["files"]


def build_cdn_urls(repo_slug: str, file_paths: List[str], revision: str = "main") -> List[str]:
    return [
        f"{HF_CDN_BASE}/{repo_slug}/resolve/{revision}/{p}"
        for p in file_paths
    ]


def pick_write_repo(primary: str, siblings: List[str], slug: str) -> str:
    """
    Deterministic repo selection to spread HF commit writes.
    """
    repos = [primary] + (siblings or [])
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(repos)
    return repos[idx]
```

---

### vanguard/backend/studio.py
```python
from __future__ import annotations

import time
from typing import Optional

try:
    from lightning import Studio, Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    Studio = None
    Teamspace = None
    LIGHTNING_AVAILABLE = False


def reuse_or_create_studio(
    name: str,
    machine: str = "L40S",
    cloud: str = "lightning-public-prod",
    idle_timeout_minutes: int = 15,
) -> Optional[Studio]:
    """
    Reuse running Studio or create one. Returns None if unavailable.
    """
    if not LIGHTNING_AVAILABLE:
        return None

    try:

