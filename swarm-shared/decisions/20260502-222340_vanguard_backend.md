# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint: missing a lightweight backend route that surfaces the top-connected hub (e.g., "MOC") and related docs for contextual task planning (`#knowledge-rag #graph #hub`).
- No HF CDN-bypass file-list strategy: surrogate-1 training will hit HF API rate limits without a pre-listed, date-scoped file manifest and CDN-only fetch path.
- No Lightning Studio reuse guard: scripts may recreate running studios and burn quota instead of reusing existing running ones (`#lightning-ai #quota`).
- No cron-safe orchestrator invocation: wrapper scripts risk exec errors if shebang/permissions/env are wrong (`#bash #script-error`).
- Missing backend API surface for frontend “Discovery Pane”: frontend expects a small JSON endpoint to bootstrap hub insight + file list without blocking.

## 2. Proposed change
Add a single backend module and minimal route:
- File: `/opt/axentx/vanguard/backend/discovery.py`
- Expose: `GET /api/discovery?date=YYYY-MM-DD`
  - Returns `{ top_hub, related_docs, hf_file_list, running_studio }`
- Keep changes under 200 lines; no DB migrations, no new secrets.

## 3. Implementation
Create `/opt/axentx/vanguard/backend/discovery.py`:

```python
# /opt/axentx/vanguard/backend/discovery.py
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

# Conditional imports (fail gracefully if deps absent)
try:
    from huggingface_hub import list_repo_tree, hf_hub_download
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False

try:
    from lightning import Studio, Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

router = APIRouter()

# Config via env (safe defaults)
HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-mirror")
HF_PATH = os.getenv("HF_DATASET_PATH", "batches/mirror-merged")
LIGHTNING_TEAMSPACE = os.getenv("LIGHTNING_TEAMSPACE", "default")
LIGHTNING_STUDIO_PREFIX = os.getenv("LIGHTNING_STUDIO_PREFIX", "surrogate-1-")

def get_top_hub_and_docs():
    """
    Minimal knowledge-rag style top-hub insight.
    Replace with real graph query when available.
    """
    # Placeholder: in practice, query your RAG/graph store.
    return {
        "hub": "MOC",
        "reason": "Most-connected node in current project graph",
        "related_docs": [
            "2026-04-27_top-hub.md",
            "20260502-222307_vanguard_backend.md",
            "20260502-222159_vanguard_backend.md",
        ],
    }

def build_hf_file_list(date_str: str) -> List[str]:
    """
    Pre-list files for one date folder via HF API (single call),
    then rely on CDN URLs during training.
    """
    if not HF_AVAILABLE:
        return []

    try:
        folder = f"{HF_PATH}/{date_str}"
        items = list_repo_tree(
            repo_id=HF_REPO,
            path=folder,
            recursive=False,
        )
        # items may be paginated; this call returns up to limit.
        files = [f.rfilename for f in getattr(items, "files", []) if f.rfilename.endswith(".parquet")]
        return sorted(files)
    except Exception as exc:
        # If rate-limited or missing, return empty; frontend can retry later.
        return []

def get_running_studio():
    """
    Reuse running Lightning Studio to save quota.
    """
    if not LIGHTNING_AVAILABLE:
        return None

    try:
        teamspace = Teamspace(name=LIGHTNING_TEAMSPACE)
        for studio in teamspace.studios:
            if studio.name.startswith(LIGHTNING_STUDIO_PREFIX) and studio.status == "running":
                return {
                    "name": studio.name,
                    "status": studio.status,
                    "url": getattr(studio, "url", None),
                }
        return None
    except Exception:
        return None

@router.get("/api/discovery")
async def discovery(
    date: str = Query(..., regex=r"^\d{4}-\d{2}-\d{2}$", description="Date folder YYYY-MM-DD")
):
    """
    Lightweight discovery endpoint for frontend.
    Returns top hub insight and HF file list for date folder.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    top_hub = get_top_hub_and_docs()
    hf_files = build_hf_file_list(date)
    studio = get_running_studio()

    # Build CDN-only URLs (no Authorization header) for frontend/training script.
    cdn_urls = [
        f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f}"
        for f in hf_files
    ]

    return {
        "date": date,
        "top_hub": top_hub,
        "hf_file_list": hf_files,
        "hf_cdn_urls": cdn_urls,
        "running_studio": studio,
        "notes": "Use CDN URLs for training to bypass HF API rate limits.",
    }
```

Wire into existing FastAPI app (likely in `/opt/axentx/vanguard/backend/main.py` or similar). Add:

```python
# In your main FastAPI file (e.g., backend/main.py)
from .discovery import router as discovery_router

app.include_router(discovery_router, prefix="", tags=["discovery"])
```

Make cron-safe launcher (if not present) at `/opt/axentx/vanguard/backend/orchestrator.py` (referenced in past decisions):

```python
#!/usr/bin/env bash
# /opt/axentx/vanguard/backend/orchestrator.py
# Cron-safe wrapper: use bash and absolute paths.

set -euo pipefail

cd /opt/axentx/vanguard

# Optional: pre-generate file list for today and store for training script
TODAY=$(date -I)
OUTFILE="backend/file_list_${TODAY}.json"

python -c "
import json, os
from backend.discovery import build_hf_file_list, get_top_hub_and_docs, get_running_studio
date = os.getenv('DATE_OVERRIDE', '${TODAY}')
data = {
    'date': date,
    'top_hub': get_top_hub_and_docs(),
    'hf_file_list': build_hf_file_list(date),
    'running_studio': get_running_studio(),
}
with open('${OUTFILE}', 'w') as f:
    json.dump(data, f, indent=2)
"
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/backend/orchestrator.py
```

Crontab entry (safe):

```cron
SHELL=/bin/bash
*/30 * * * * cd /opt/axentx/vanguard && python -m backend.orchestrator >> /var/log/vanguard-discovery.log 2>&1
```

## 4. Verification
1. Start backend (e.g., `uvicorn backend.main:app --host 0.0.0.0 --port 8000`).
2. Query locally:
   ```bash
   curl "http://localhost:8000/api/discovery?date=2026-05-02"
   ```
   Expect JSON with `top_hub`, `hf_file_list`, `hf_cdn_urls`, and `running_studio` (or `null`).
3. Confirm CDN URLs are valid (one HEAD request):
   ```bash
   curl -I "https://huggingface.co/datasets/axentx/surrogate-1-mirror/resolve/main/batches/mirror-merged/2026-05-02/<file>.parquet"
   ```
   Should return `200` or `404` (not `401`/`429` from API).
4. Run orchestrator manually and check file output:
   ```bash
   cd /opt/ax
