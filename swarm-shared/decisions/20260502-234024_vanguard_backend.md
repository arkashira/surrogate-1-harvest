# vanguard / backend

**Final Actionable Improvement: Unified Persistent File Manifest + Studio Cache**

**Diagnosis (merged, de-duplicated)**
- No persisted file manifest → every run re-lists repos and re-checks schemas, causing HF API 429 (1000 req/5 min).
- Lightning Studio is recreated instead of reused, burning quota on recreation.
- Repeated `list_repo_tree` / `load_dataset` / schema checks waste quota and slow ingestion.
- Missing Kaggle KGAT token auth causes 401 errors.
- No TTL/staleness policy leads to stale caches or unnecessary refreshes.

**Single Chosen Solution**
Implement a small, robust caching layer with two responsibilities:
1. **FileManifestCache**: persist repo file listings + schemas with TTL to eliminate redundant HF API calls.
2. **StudioCache**: persist and reuse a Lightning Studio ID to avoid recreation.

Add required auth for Kaggle KGAT. Keep implementation minimal, testable, and safe for concurrent runs.

---

**Implementation**

1) Add `vanguard/cache.py`
```python
# vanguard/cache.py
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

CACHE_DIR = Path(os.getenv("VANGUARD_CACHE_DIR", ".vanguard_cache"))
CACHE_DIR.mkdir(exist_ok=True)

_MANIFEST_FILE = CACHE_DIR / "file_manifest.json"
_STUDIO_FILE = CACHE_DIR / "studio.json"


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w") as f:
        json.dump(payload, f, indent=2)


# ---- File manifest cache ----
def get_file_manifest(repo_id: str, ttl_seconds: int = 300) -> Optional[Dict[str, Any]]:
    """
    Returns cached manifest if present and fresh, else None.
    Structure:
    {
      "repo:<repo_id>": {
        "last_updated": 1712345678.123,
        "files": {
          "path/to/file": { "schema": {...}, "size": 1234 }
        }
      }
    }
    """
    cache = _load_json(_MANIFEST_FILE)
    key = f"repo:{repo_id}"
    entry = cache.get(key)
    if not entry:
        return None
    last = entry.get("last_updated", 0)
    if time.time() - last > ttl_seconds:
        return None
    return entry.get("files")


def save_file_manifest(repo_id: str, files: Dict[str, Any]) -> None:
    cache = _load_json(_MANIFEST_FILE)
    key = f"repo:{repo_id}"
    cache[key] = {
        "last_updated": time.time(),
        "files": files,
    }
    _save_json(_MANIFEST_FILE, cache)


# ---- Studio cache ----
def get_cached_studio() -> Optional[str]:
    data = _load_json(_STUDIO_FILE)
    studio_id = data.get("studio_id")
    if not studio_id:
        return None
    # Lightweight liveness: file exists => reuse. Caller must validate via API if needed.
    return studio_id


def save_studio(studio_id: str) -> None:
    _save_json(_STUDIO_FILE, {"studio_id": studio_id, "saved_at": time.time()})


def clear_studio() -> None:
    if _STUDIO_FILE.exists():
        _STUDIO_FILE.unlink()
```

2) Add `vanguard/hf_repo.py` (core repo listing + manifest logic)
```python
# vanguard/hf_repo.py
import os
from typing import Dict
from huggingface_hub import list_repo_tree

from vanguard.cache import get_file_manifest, save_file_manifest


def build_file_manifest(repo_id: str) -> Dict[str, Any]:
    """
    Build manifest by walking repo tree.
    Avoids per-file schema calls in this step to reduce quota.
    Schema checks can be done lazily per file as needed.
    """
    files: Dict[str, Any] = {}
    for path in list_repo_tree(repo_id, recursive=True):
        # path is str like "data/train.parquet"
        # size not provided by list_repo_tree; callers can stat lazily if required.
        files[path] = {"schema": None}  # placeholder; populate lazily or via get_file_schema
    return files


def get_repo_files(repo_id: str, ttl_seconds: int = 300) -> Dict[str, Any]:
    cached = get_file_manifest(repo_id, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached

    files = build_file_manifest(repo_id)
    save_file_manifest(repo_id, files)
    return files
```

3) Update training entrypoint (`train.py` or equivalent)
```python
# train.py (excerpt)
from vanguard.hf_repo import get_repo_files
from vanguard.cache import get_cached_studio, save_studio
from vanguard.lightning_studio import get_or_create_studio  # your existing helper

REPO_ID = os.getenv("HF_REPO_ID", "your-org/your-repo")

def train():
    # 1) Use cached file manifest to avoid list_repo_tree on every run
    files = get_repo_files(REPO_ID, ttl_seconds=300)
    print(f"Found {len(files)} files (cached or fresh).")

    # 2) Reuse Lightning Studio if available
    studio_id = get_cached_studio()
    if studio_id:
        print(f"Reusing studio: {studio_id}")
        studio = get_or_create_studio(studio_id=studio_id)  # should support reuse
    else:
        print("Creating new studio.")
        studio = get_or_create_studio()
        save_studio(studio.id)

    # 3) Proceed with training using `files` manifest
    # ...
```

4) Add Kaggle KGAT auth fix (minimal, non-breaking)
```python
# vanguard/kaggle_auth.py
import os
from pathlib import Path

def get_kaggle_kgat_token() -> str:
    # Prefer env var; fallback to kaggle.json if present
    token = os.getenv("KAGGLE_KGAT_TOKEN")
    if token:
        return token

    kaggle_json = Path.home() / ".kaggle" / "kaggle.json"
    if kaggle_json.exists():
        import json
        with kaggle_json.open() as f:
            creds = json.load(f)
        # If kaggle.json contains a token-like field, use it
        if "kgat_token" in creds:
            return creds["kgat_token"]
        # Otherwise, compose Authorization header from username/key if needed by client
        # (implementation depends on KGAT client)
    raise RuntimeError("Kaggle KGAT token not found. Set KAGGLE_KGAT_TOKEN or provide kaggle.json with kgat_token.")
```
Ensure your KGAT client uses:
```
Authorization: Bearer <kgat_token>
```

---

**Verification Plan**

1) Manifest cache
- Run once: confirm `.vanguard_cache/file_manifest.json` created with repo files.
- Run again within 5 min: confirm no `list_repo_tree` calls (check logs or HF API usage).
- Wait >5 min or touch manifest with old timestamp: confirm refresh on next run.

2) Studio reuse
- After first run, confirm `.vanguard_cache/studio.json` exists with `studio_id`.
- Second run: confirm log shows “Reusing studio” and no new studio creation.

3) Rate limits
- Monitor HF API usage (or count list_repo_tree calls) to confirm quota burn reduced.

4) Kaggle KGAT
- With `KAGGLE_KGAT_TOKEN` unset and no kaggle.json: expect clear RuntimeError.
- With token present: expect 200/401 from KGAT endpoint (not 401 due to missing token).

---

**Why this resolves contradictions and is most actionable**
- Combines the best parts: TTL-based manifest (C1), explicit cache class + reuse for Studio (C2
