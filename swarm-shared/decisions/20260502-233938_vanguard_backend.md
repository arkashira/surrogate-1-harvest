# vanguard / backend

**Final Synthesized Implementation**  
*(Best parts of all three proposals, contradictions resolved for correctness + concrete actionability)*

---

## 1. Diagnosis (agreed across all candidates)
- No persistent file-list cache → repeated `list_repo_tree` / `load_dataset` → HF API 429 (1000 req/5 min).
- Lightning Studio lifecycle recreates environment → cache not reused → quota burned.
- Training re-lists repository files every run.
- Inconsistent file-list management → risk of corruption or stale reads.

**Resolution priority**: Correctness first (avoid data corruption), then concrete actionability (simple, testable steps).

---

## 2. Chosen Design (synthesis)
- **Single, project-level JSON cache file** (not per-file cache files) to avoid filesystem spam and simplify CI/CD / Studio lifecycle.
- **Per-repository cache entries** (supports multiple repos).
- **Cache key**: `repo_id + path + recursive` (normalized).
- **Integrity checks**:
  - `last_updated` timestamp (ISO 8601).
  - Optional `etag`/`sha` for file list (defensive, not required for MVP).
- **Expiration policy** (simple TTL default 1 hour; configurable).
- **Explicit invalidation hooks** on known repo updates (push event, manual flag, or training script override).
- **Graceful fallback**: if cache is corrupt/missing/expired, call HF API and repopulate cache.

**Why not Candidate 3’s per-file cache?**  
Correctness: too many small files in ephemeral Studio environments; harder to invalidate atomically.  
Actionability: single JSON is easier to inspect, test, and version-control exclude.

**Why not Candidate 2’s flat file list?**  
Correctness: loses per-repo scoping and recursive vs non-recursive distinction → collisions.  
Actionability: per-repo structure is safer and clearer.

---

## 3. Implementation (concrete, minimal, testable)

### 3.1 Create `file_list_cache.json` (project root)
```json
{
  "ttl_seconds": 3600,
  "repositories": {
    "my-org/my-repo": {
      "/data/train": {
        "recursive": true,
        "last_updated": "2025-01-01T12:00:00Z",
        "file_list": ["train/001.txt", "train/002.txt"],
        "sha256": "abc123..."
      }
    }
  }
}
```

### 3.2 Add `vanguard/cache.py`
```python
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import hashlib

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "file_list_cache.json")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _is_expired(entry: Dict[str, Any], ttl_seconds: int) -> bool:
    try:
        last = datetime.fromisoformat(entry["last_updated"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last > timedelta(seconds=ttl_seconds)
    except Exception:
        return True

def _hash_file_list(file_list: List[str]) -> str:
    return hashlib.sha256(json.dumps(file_list, sort_keys=True).encode()).hexdigest()

def load_cache() -> Dict[str, Any]:
    if not os.path.exists(CACHE_PATH):
        return {"ttl_seconds": 3600, "repositories": {}}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ttl_seconds": 3600, "repositories": {}}

def save_cache(cache: Dict[str, Any]) -> None:
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    os.replace(tmp, CACHE_PATH)

def get_cached_file_list(repo: str, path: str, recursive: bool) -> Optional[List[str]]:
    cache = load_cache()
    repo_entry = cache.get("repositories", {}).get(repo, {})
    key = f"{path}?recursive={recursive}"
    entry = repo_entry.get(key)
    if not entry:
        return None
    ttl = cache.get("ttl_seconds", 3600)
    if _is_expired(entry, ttl):
        return None
    # Optional integrity check
    expected = entry.get("sha256")
    if expected and expected != _hash_file_list(entry.get("file_list", [])):
        return None
    return entry.get("file_list")

def set_cached_file_list(repo: str, path: str, recursive: bool, file_list: List[str]) -> None:
    cache = load_cache()
    repo_entry = cache.setdefault("repositories", {}).setdefault(repo, {})
    key = f"{path}?recursive={recursive}"
    repo_entry[key] = {
        "recursive": recursive,
        "last_updated": _now_iso(),
        "file_list": file_list,
        "sha256": _hash_file_list(file_list),
    }
    save_cache(cache)

def invalidate_cache(repo: Optional[str] = None, path: Optional[str] = None, recursive: Optional[bool] = None) -> None:
    cache = load_cache()
    repos = cache.get("repositories", {})
    if repo is None:
        cache["repositories"] = {}
    elif path is None:
        repos.pop(repo, None)
    else:
        key = f"{path}?recursive={recursive}"
        if repo in repos and key in repos[repo]:
            del repos[repo][key]
    save_cache(cache)
```

### 3.3 Update `vanguard/utils.py`
```python
from .cache import get_cached_file_list, set_cached_file_list

def list_repo_tree(repo: str, path: str, recursive: bool = False) -> List[str]:
    cached = get_cached_file_list(repo, path, recursive)
    if cached is not None:
        return cached

    # Real HF API call (pseudocode)
    file_list = api.list_repo_tree(repo, path, recursive)  # type: ignore
    set_cached_file_list(repo, path, recursive, file_list)
    return file_list
```

### 3.4 Update `vanguard/train.py`
```python
from .cache import get_cached_file_list, set_cached_file_list
from .utils import list_repo_tree

def train(repo: str, data_path: str, force_refresh: bool = False) -> None:
    if force_refresh:
        from .cache import invalidate_cache
        invalidate_cache(repo=repo, path=data_path, recursive=True)

    file_list = list_repo_tree(repo, data_path, recursive=True)
    # Proceed with training using file_list
    ...
```

---

## 4. Verification (concrete steps)
1. **Cache creation**: run `train.py` once; confirm `file_list_cache.json` exists and contains correct repo/path entry.
2. **Cache hit**: run again within TTL; confirm no HF API call (monitor logs or count requests).
3. **Cache expiry**: set `ttl_seconds` to 1, wait >1s, run again; confirm new API call and cache update.
4. **Integrity failure**: manually corrupt `sha256` in JSON; run; confirm fallback to API and repair.
5. **Invalidate**: call `invalidate_cache(repo="my-org/my-repo")`; confirm entry removed and fresh API call on next run.
6. **Studio lifecycle**: stop/start Studio; confirm cache file persists (if mounted storage) and is reused.

---

## 5. Notes on Contradictions (resolved)
- **Per-file vs single JSON**: single JSON chosen for correctness (atomic updates, simpler invalidation) and actionability (easy to inspect).
- **Cache location**: project root (not per-module) to maximize reuse across Studio recreations when persistent storage is mounted.
- **Expiration**: simple TTL (1h default) + explicit invalidation. Avoids complex multi-tier cache (Candidate 3) which adds little value for this use-case and complicates Studio ephemeral runs.
