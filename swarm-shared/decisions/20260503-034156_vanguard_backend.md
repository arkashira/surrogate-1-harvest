# vanguard / backend

## 1. Diagnosis
- Backend ingestion/training jobs still perform runtime `list_repo_tree`/`load_dataset` calls → 429 risk and non-reproducible runs.
- No deterministic, content-addressed manifest keyed by `{date}/{slug}` → jobs re-enumerate and can’t guarantee CDN-only fetches.
- Missing single-file manifest generation step after market-analysis/knowledge-rag runs (per top-hub insight pattern).
- No guard to prevent frontend/backend from triggering HF API during dataset selection/preview.
- No reuse strategy for Lightning Studio across training iterations → quota waste.

## 2. Proposed change
Add a backend module that:
- Exposes `/v1/manifest/generate` (POST) to create a deterministic manifest for a given HF dataset + date folder.
- Stores manifest as JSON at `manifests/{dataset}/{date}.json` with entries `{slug, path, cdn_url, sha256}`.
- Exposes `/v1/manifest/{dataset}/{date}` (GET) to serve the manifest so frontend/backend can use CDN-only URLs.
- Adds a lightweight CLI `scripts/generate_manifest.py` for cron/one-off runs.
- Adds a pre-flight check in training launcher to reuse running Lightning Studio.

Scope:
- New file: `/opt/axentx/vanguard/backend/routes/manifest.py`
- New file: `/opt/axentx/vanguard/backend/services/manifest_service.py`
- New file: `/opt/axentx/vanguard/scripts/generate_manifest.py`
- Update: `/opt/axentx/vanguard/backend/main.py` (include router)
- Update: `/opt/axentx/vanguard/backend/lightning/launcher.py` (add studio reuse)

## 3. Implementation

### backend/routes/manifest.py
```python
# /opt/axentx/vanguard/backend/routes/manifest.py
from fastapi import APIRouter, HTTPException
from backend.services.manifest_service import generate_manifest, get_manifest

router = APIRouter(prefix="/v1/manifest", tags=["manifest"])

@router.post("/generate")
def generate(dataset: str, date: str):
    """
    Generate a deterministic manifest for dataset/date.
    Example: {"dataset": "mycorp/mirror", "date": "2026-04-27"}
    """
    try:
        manifest = generate_manifest(dataset=dataset, date=date)
        return {"ok": True, "dataset": dataset, "date": date, "count": len(manifest)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/{dataset}/{date}")
def read(dataset: str, date: str):
    manifest = get_manifest(dataset=dataset, date=date)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifest not found. Generate first.")
    return manifest
```

### backend/services/manifest_service.py
```python
# /opt/axentx/vanguard/backend/services/manifest_service.py
import json
import hashlib
import os
from pathlib import Path
from typing import List, Dict, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download

HF_API = HfApi()
MANIFEST_ROOT = Path(__file__).parent.parent.parent / "manifests"
MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)

def _cdn_url(dataset: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{dataset}/resolve/main/{path}"

def _sha256_url(url: str) -> Optional[str]:
    """Best-effort ETag-like hash from CDN HEAD (no auth)."""
    try:
        r = requests.head(url, timeout=10)
        r.raise_for_status()
        # Use ETag if present, else fallback to size+last-modified
        etag = r.headers.get("ETag")
        if etag:
            return etag.strip('"')
        return hashlib.sha256(f"{r.headers.get('Content-Length')}:{r.headers.get('Last-Modified')}".encode()).hexdigest()
    except Exception:
        return None

def generate_manifest(dataset: str, date: str) -> List[Dict]:
    """
    Single API call to list one date folder, then produce manifest.
    Avoids recursive listing and repeated API calls during training.
    """
    # List only the target date folder (non-recursive).
    tree = HF_API.list_repo_tree(repo_id=dataset, path=date, recursive=False)
    entries = []
    for item in tree:
        if item.type != "file":
            continue
        path = item.path
        slug = Path(path).stem
        url = _cdn_url(dataset, path)
        entry = {
            "dataset": dataset,
            "date": date,
            "slug": slug,
            "path": path,
            "cdn_url": url,
            "sha256": _sha256_url(url),
        }
        entries.append(entry)

    # Deterministic ordering for reproducibility.
    entries.sort(key=lambda x: (x["date"], x["slug"]))

    out_path = MANIFEST_ROOT / dataset.replace("/", "_") / f"{date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, indent=2))
    return entries

def get_manifest(dataset: str, date: str) -> Optional[List[Dict]]:
    out_path = MANIFEST_ROOT / dataset.replace("/", "_") / f"{date}.json"
    if not out_path.exists():
        return None
    return json.loads(out_path.read_text())
```

### scripts/generate_manifest.py
```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/scripts/generate_manifest.py
# Usage: bash scripts/generate_manifest.py <dataset> <date>
set -euo pipefail

cd "$(dirname "$(dirname "$(realpath "$0")")")"

DATASET="${1:-mycorp/mirror}"
DATE="${2:-$(date +%Y-%m-%d)}"

python -m backend.routes.manifest --dataset "$DATASET" --date "$DATE" 2>/dev/null || \
python -c "
import sys
sys.path.insert(0, '.')
from backend.services.manifest_service import generate_manifest
m = generate_manifest(dataset='$DATASET', date='$DATE')
print('Generated', len(m), 'entries for', '$DATASET', '$DATE')
"
```
(Also provide a small Python CLI alternative if preferred; above uses inline Python for portability.)

### backend/lightning/launcher.py (add studio reuse)
```python
# /opt/axentx/vanguard/backend/lightning/launcher.py
from lightning import Lightning, Teamspace, Studio, Machine

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    # If stopped, restart instead of creating new to save quota.
    for s in teamspace.studios:
        if s.name == name:
            s.start(machine=machine)
            return s
    return Studio.create(name=name, machine=machine, create_ok=True)
```

### backend/main.py (wire router)
```python
# /opt/axentx/vanguard/backend/main.py
from fastapi import FastAPI
from backend.routes.manifest import router as manifest_router

app = FastAPI()
app.include_router(manifest_router)
# ... existing routes
```

## 4. Verification
1. Generate manifest (single API call):
   ```bash
   curl -X POST "http://localhost:8000/v1/manifest/generate?dataset=mycorp/mirror&date=2026-04-27"
   ```
   Expect `{"ok":true,"count":N}` and file created at `manifests/mycorp_mirror/2026-04-27.json`.

2. Fetch manifest:
   ```bash
   curl "http://localhost:8000/v1/manifest/mycorp/mirror/2026-04-27"
   ```
   Expect JSON array with `cdn_url` entries (no Authorization required).

3. Confirm CDN-only usage:
   - Pick one `cdn_url` from manifest and `curl -I` — should return 200
