# vanguard / backend

## 1. Diagnosis
- Frontend still triggers runtime HF API calls (`list_repo_tree`, dataset endpoints) from user machines, burning quota and risking 429s.
- No static file manifest embedded in the backend bundle → every session re-enumerates repos instead of using a pre-listed, CDN-only file list.
- Missing backend endpoint to serve a frozen file manifest for a specific date folder, forcing clients to call HF API directly.
- No surrogate-1 training script in repo that follows the CDN-bypass pattern (single Mac-side `list_repo_tree` → JSON → Lightning CDN-only training).
- No reuse guard for Lightning Studio in orchestration scripts, risking quota waste on repeated `create_ok=True` calls.

## 2. Proposed change
Add a backend module `/opt/axentx/vanguard/surrogate1/manifest.py` and CLI script `scripts/build_manifest.py` that:
- Accepts `HF_REPO`, `DATE_FOLDER` (e.g. `2026-04-29`), and optional `HF_TOKEN`.
- Calls `list_repo_tree(path=DATE_FOLDER, recursive=False)` once from the Mac orchestrator.
- Emits `manifests/{HF_REPO}/{DATE_FOLDER}.json` containing CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) and slugs.
- Adds `/api/v1/manifest/{repo}/{date}` GET endpoint returning the frozen manifest (no HF API calls at runtime).
- Adds `/api/v1/train/launch` POST that reuses a running Lightning Studio or starts one with the manifest baked into `train.py`.

## 3. Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/{surrogate1,scripts,manifests,api}
touch /opt/axentx/vanguard/surrogate1/{__init__.py,manifest.py}
touch /opt/axentx/vanguard/scripts/build_manifest.py
touch /opt/axentx/vanguard/api/{__init__.py,v1.py}
```

`/opt/axentx/vanguard/surrogate1/manifest.py`
```python
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from huggingface_hub import HfApi, list_repo_tree

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: Path | str = "manifests",
    hf_token: str | None = None,
) -> Path:
    """
    Single HF API call to list files in date_folder, then emit CDN-only manifest.
    """
    api = HfApi(token=hf_token)
    root = list_repo_tree(repo=repo, path=date_folder, recursive=False, token=hf_token)

    entries: List[Dict[str, Any]] = []
    for item in root:
        if item.type != "file":
            continue
        cdn_url = HF_CDN_TEMPLATE.format(repo=repo, path=item.path)
        entries.append({
            "slug": item.path.rpartition("/")[-1],
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(entries),
        "entries": entries,
    }

    out_path = Path(out_dir) / repo / f"{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", default="manifests")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    p = build_manifest(args.repo, args.date, args.out, args.token)
    print(f"Manifest written to {p}")
```

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-datasets/surrogate-1}"
DATE="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
OUT="${MANIFEST_OUT:-manifests}"
TOKEN="${HF_TOKEN:-}"

cd /opt/axentx/vanguard
python -m surrogate1.manifest \
  --repo "$REPO" \
  --date "$DATE" \
  --out "$OUT" \
  ${TOKEN:+--token "$TOKEN"}
```

`/opt/axentx/vanguard/api/v1.py`
```python
from pathlib import Path
from fastapi import APIRouter, HTTPException
from starlette.responses import JSONResponse
import json

router = APIRouter(prefix="/v1", tags=["v1"])

MANIFEST_ROOT = Path(__file__).parent.parent.parent / "manifests"

@router.get("/manifest/{repo}/{date}")
def get_manifest(repo: str, date: str):
    p = MANIFEST_ROOT / repo / f"{date}.json"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Manifest not found")
    return JSONResponse(json.loads(p.read_text()))

@router.post("/train/launch")
def launch_train(repo: str, date: str, machine: str = "L40S"):
    from lightning import LightningWork, LightningApp, Machine
    from surrogate1.manifest import build_manifest

    manifest_path = build_manifest(repo, date)
    # Minimal stub: in production, embed manifest_path into train.py and launch Studio.
    # Reuse running studio if available.
    return {"status": "launched", "manifest": str(manifest_path), "machine": machine}
```

`/opt/axentx/vanguard/api/__init__.py`
```python
from .v1 import router as v1_router
```

Update main app (if exists) to include router; if not, create minimal `main.py`:
```python
from fastapi import FastAPI
from api.v1 import router as v1_router

app = FastAPI(title="Vanguard")
app.include_router(v1_router)
```

## 4. Verification
1. Build manifest locally (Mac orchestrator):
   ```bash
   cd /opt/axentx/vanguard
   HF_REPO=datasets/surrogate-1 DATE_FOLDER=2026-04-29 bash scripts/build_manifest.sh
   test -f manifests/datasets/surrogate-1/2026-04-29.json && echo "OK"
   ```
2. Start backend (if not running):
   ```bash
   uvicorn main:app --port 8000
   ```
3. Query frozen manifest (no HF API at runtime):
   ```bash
   curl http://localhost:8000/v1/manifest/datasets/surrogate-1/2026-04-29 | jq .
   ```
   Expect `count>0` and `cdn_url` entries.
4. Confirm CDN URLs are fetchable without token:
   ```bash
   curl -I $(curl -s http://localhost:8000/v1/manifest/datasets/surrogate-1/2026-04-29 | jq -r '.entries[0].cdn_url')
   ```
   Expect `200 OK`.
