# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

**Core diagnosis (merged, deduped):**
- No persisted `(repo, dateFolder)` manifest → frontend and training scripts re-enumerate HF API on every run, burning quota and triggering 429s/128-commit-per-hour limits.
- Backend uses authenticated `/api/` paths instead of public CDN for dataset files, adding avoidable rate-limit pressure.
- No single source-of-truth file list → schema heterogeneity and repeated `list_repo_tree` calls across training/ingest jobs.
- No lightweight endpoint to serve the manifest → frontend can’t cache or paginate efficiently.
- Missing CDN-bypass download path forces all file access through HF API during data preparation.

**Proposed change (merged, concrete):**
Add a backend manifest generator + CDN-bypass utility and one endpoint:
- `/opt/axentx/vanguard/backend/manifest.py` (new) — generate/persist/load manifests and produce CDN URLs.
- `/opt/axentx/vanguard/backend/api.py` (extend) — add `/manifest` endpoint to return or generate-once the manifest.
- All file fetches (frontend and training) use public CDN: `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth/rate-limit cost).
- Manifest is generated once per `(repo, dateFolder)` via a single `list_repo_tree` call (non-recursive) and reused.

---

### Implementation

```bash
# Ensure backend directory exists
mkdir -p /opt/axentx/vanguard/backend
```

`/opt/axentx/vanguard/backend/manifest.py`
```python
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"


def list_datefolder_files(
    repo: str,
    date_folder: str,
    token: Optional[str] = None,
    recursive: bool = False,
) -> List[Dict[str, object]]:
    """
    Single API call to list files in repo/date_folder.
    Returns list of dicts with at least {'path', 'size', 'type'}.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{HF_API_BASE}/datasets/{repo}/tree/{date_folder}"
    params = {"recursive": int(recursive)}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    items = resp.json()
    # Normalize
    files = [
        {
            "path": item["path"],
            "size": item.get("size", 0),
            "type": item.get("type", "file"),
        }
        for item in items
        if item.get("type") == "file"
    ]
    return files


def build_manifest(
    repo: str,
    date_folder: str,
    output_dir: Path,
    token: Optional[str] = None,
    recursive: bool = False,
) -> Path:
    """
    Persist manifest JSON for (repo, date_folder).
    Manifest schema:
    {
      "repo": "...",
      "date_folder": "...",
      "generated_at_utc": "...",
      "files": [{"path": "...", "size": 0, "type": "file"}],
      "cdn_base": "https://huggingface.co/datasets/{repo}/resolve/main"
    }
    """
    files = list_datefolder_files(repo=repo, date_folder=date_folder, token=token, recursive=recursive)
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "files": files,
        "cdn_base": f"{HF_CDN_BASE}/{repo}/resolve/main",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    # Safe filename: replace path separators
    safe_folder = date_folder.replace("/", "__")
    out_path = output_dir / f"{repo.replace('/', '_')}__{safe_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out_path


def cdn_download_url(repo: str, path: str) -> str:
    """
    Public CDN URL that bypasses HF API auth/rate limits.
    """
    return f"{HF_CDN_BASE}/{repo}/resolve/main/{path}"


def load_manifest(repo: str, date_folder: str, manifest_dir: Path) -> Dict:
    safe_folder = date_folder.replace("/", "__")
    p = manifest_dir / f"{repo.replace('/', '_')}__{safe_folder}.json"
    if not p.is_file():
        raise FileNotFoundError(f"Manifest not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
```

`/opt/axentx/vanguard/backend/api.py` (append/integrate into existing FastAPI app)
```python
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .manifest import build_manifest, load_manifest

router = APIRouter()
MANIFEST_DIR = Path(os.getenv("VANGUARD_MANIFEST_DIR", "/opt/axentx/vanguard/data/manifests"))
HF_TOKEN = os.getenv("HF_TOKEN")  # optional; only needed for private repos

@router.get("/manifest")
def get_or_build_manifest(
    repo: str = Query(..., description="HF dataset repo, e.g. 'username/dataset'"),
    date_folder: str = Query(..., description="Folder under repo, e.g. 'batches/2026-05-03'"),
    rebuild: bool = Query(False, description="Force rebuild manifest"),
    recursive: bool = Query(False, description="List tree recursively"),
):
    """
    Return persisted manifest for (repo, date_folder).
    If missing or rebuild=True, generate via a single list_repo_tree call.
    """
    try:
        manifest_dir = MANIFEST_DIR
        safe_folder = date_folder.replace("/", "__")
        manifest_path = manifest_dir / f"{repo.replace('/', '_')}__{safe_folder}.json"

        if rebuild or not manifest_path.is_file():
            build_manifest(
                repo=repo,
                date_folder=date_folder,
                output_dir=manifest_dir,
                token=HF_TOKEN,
                recursive=recursive,
            )
        manifest = load_manifest(repo=repo, date_folder=date_folder, manifest_dir=manifest_dir)
        return {"ok": True, "manifest": manifest}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
```

Usage in training/ingestion script (example snippet)
```python
from pathlib import Path
from vanguard.backend.manifest import load_manifest, cdn_download_url
import requests

manifest = load_manifest(
    repo="org/surrogate-1",
    date_folder="batches/2026-05-03",
    manifest_dir=Path("data/manifests"),
)
for f in manifest["files"]:
    url = cdn_download_url(repo=manifest["repo"], path=f["path"])
    # CDN download (no Authorization header) — bypasses API rate limits
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    # process bytes...
```

---

### Verification

1. Generate manifest once:
   ```bash
   HF_TOKEN=hf_xxx python -c "from vanguard.backend.manifest import build_manifest; build_manifest(repo='org/surrogate-1', date_folder='batches/2026-05-03', output_dir='data/manifests', token='hf_xxx')"
   ```
   Confirm `data/manifests/org_surrogate-1__batches__2026-05-03.json` exists and contains `files` array and `cdn_base`.

2. Start backend and hit endpoint:
   ```bash
   curl "http://localhost:8000/manifest?repo=org/surrogate-1&date_folder=batches/2026-05-03"
   ```
   Expect `{"ok":true,"manifest":{...}}
