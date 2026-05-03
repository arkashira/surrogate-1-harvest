# vanguard / backend

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest → backend re-enumerates HF API on every request, burning quota and risking 429s.
- Data fetches use authenticated API paths instead of public CDN → unnecessary auth overhead and tighter rate limits.
- Training/data scripts likely re-list repos per run → amplifies commit-cap and rate-limit pressure.
- Missing deterministic repo selection for writes → HF 128-commit-per-hour cap can block ingestion bursts.
- No studio reuse guard → Lightning quota burned by repeated create/destroy cycles during iteration.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/manifest.py` and wire it into the main backend module so that:
- A single `list_repo_tree` call (per date folder) is cached to disk as `manifests/{repo}/{date}.json`.
- All downstream code uses CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for reads.
- Deterministic repo shard selection via hash(slug) % N siblings.
- Expose `get_manifest(repo, date)` + `get_cdn_url(repo, path)` helpers.

Scope: new file + light edits to `backend/__init__.py` (or main server file) to import and preload manifests on startup.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/manifest.py
import json
import hashlib
import os
from pathlib import Path
from typing import List, Optional, Dict, Any

from huggingface_hub import list_repo_tree, hf_hub_download

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

# Deterministic sibling repo selection for HF commit-cap mitigation
HF_SIBLING_REPOS = [
    "axentx/vanguard-dataset",
    "axentx/vanguard-dataset-s1",
    "axentx/vanguard-dataset-s2",
    "axentx/vanguard-dataset-s3",
    "axentx/vanguard-dataset-s4",
]

def _shard_repo(slug: str) -> str:
    """Pick sibling repo deterministically to spread writes."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(HF_SIBLING_REPOS)
    return HF_SIBLING_REPOS[idx]

def _manifest_path(repo: str, date: str) -> Path:
    safe_repo = repo.replace("/", "_")
    return MANIFEST_DIR / f"{safe_repo}_{date}.json"

def get_manifest(repo: str, date: str, *, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Return cached manifest for repo/date or create it via single API call.
    Manifest entries contain minimal metadata + path.
    """
    p = _manifest_path(repo, date)
    if not force_refresh and p.exists():
        return json.loads(p.read_text())

    # Single API call: non-recursive top-level of date folder
    tree = list_repo_tree(repo=repo, path=date, recursive=False)
    entries = [{"path": item["path"], "type": item["type"]} for item in tree if item["type"] == "file"]
    p.write_text(json.dumps(entries, indent=2))
    return entries

def get_cdn_url(repo: str, path: str) -> str:
    """Public CDN URL that bypasses HF API auth/rate limits."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def get_hf_hub_download_path(repo: str, filename: str, **kwargs) -> str:
    """Local cached download path via hf_hub_download (for authenticated/private repos)."""
    return hf_hub_download(repo_id=repo, filename=filename, **kwargs)

def pick_sibling_for_write(slug: str) -> str:
    """Pick sibling repo for writing to mitigate HF 128-commit/hr cap."""
    return _shard_repo(slug)
```

```python
# /opt/axentx/vanguard/backend/__init__.py  (or server.py if that's the main module)
# Add at top after imports:
from .manifest import get_manifest, get_cdn_url, pick_sibling_for_write

__all__ = ["get_manifest", "get_cdn_url", "pick_sibling_for_write", ...]
```

If there’s an existing FastAPI/Flask app, add a warm-up route or startup event to preload today’s manifests (optional):

```python
# Example for FastAPI in server.py
from .manifest import get_manifest

@app.on_event("startup")
def warm_manifests():
    # Lightweight: only preload known active repos/dates to avoid cold 429s
    for repo in ["axentx/vanguard-dataset"]:
        try:
            get_manifest(repo, "2026-05-03", force_refresh=False)
        except Exception:
            pass  # tolerate transient; will create on first use
```

## 4. Verification

1. Run once (Mac orchestration host):
   ```bash
   cd /opt/axentx/vanguard
   python -c "from backend.manifest import get_manifest, get_cdn_url; ms = get_manifest('axentx/vanguard-dataset', '2026-05-03'); print('entries:', len(ms)); print('cdn sample:', get_cdn_url('axentx/vanguard-dataset', ms[0]['path']) if ms else 'none')"
   ```
   - Expect: JSON file created under `manifests/`, printed CDN URL with `resolve/main/`.

2. Confirm CDN bypass behavior:
   ```bash
   curl -I "$(python -c "from backend.manifest import get_cdn_url; print(get_cdn_url('axentx/vanguard-dataset', '2026-05-03/somefile.parquet'))")"
   ```
   - Expect: `200 OK` or `404` (if file absent) without auth redirects.

3. Confirm manifest reuse (no new API calls):
   - Delete manifest file, run the Python snippet above twice; second run should not invoke network (check `lsof` or timestamps).

4. Confirm sibling selection is deterministic:
   ```bash
   python -c "from backend.manifest import pick_sibling_for_write; print(pick_sibling_for_write('test-slug'))"
   ```
   - Expect: stable repo across repeated calls.

5. Integration check: start backend server and load a page/route that uses manifests; verify no HF API 429s appear in logs and CDN URLs are used for data fetches.
