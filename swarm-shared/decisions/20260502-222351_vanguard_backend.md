# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint → planning is ad-hoc and violates `#knowledge-rag #graph #hub`.
- Missing HF CDN-bypass file list for surrogate-1 training → future training jobs will hit 429 rate limits during data loading.
- No Lightning Studio reuse guard → risk of burning 80hr/mo quota by recreating running studios.
- Orchestrator exists but lacks safe, idempotent CLI entrypoint and cron hygiene (no `SHELL=/bin/bash`, no shebang discipline).
- No lightweight backend API to serve file-list + hub insight to frontend (frontend currently blocked on backend).

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/api.py` (FastAPI) + extend `/opt/axentx/vanguard/backend/orchestrator.py` with safe CLI and CDN-bypass file-list generation. Expose:
- `GET /file-list/{date}` → returns JSON list of dataset file paths for a date folder (cached, CDN URLs).
- `GET /hub/{hub_name}` → returns top-hub insight (stub for RAG).
- CLI: `--generate-file-list --date YYYY-MM-DD` and `--serve` (dev).

## 3. Implementation

### backend/orchestrator.py
```python
#!/usr/bin/env python3
"""
Cron-safe orchestrator for vanguard backend.
Usage:
  python -m backend.orchestrator --generate-file-list --date 2026-05-02
  python -m backend.orchestrator --serve
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List

HF_REPO = "datasets/surrogate-1"  # adjust as needed
BASE_CDN = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

VANGUARD_ROOT = Path(__file__).parent.parent.parent
CACHE_DIR = VANGUARD_ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    list_repo_tree = None  # degrade gracefully; file list can be provided manually

def _today_str() -> str:
    return date.today().isoformat()

def generate_file_list_for_date(target_date: str, out_path: Path) -> List[str]:
    """
    Single API call to list top-level folder for target_date (non-recursive),
    then build CDN URLs. Returns list of CDN URLs.
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed; cannot list repo tree.")

    # Expect folder layout: data/{target_date}/...
    folder_path = f"data/{target_date}"
    items = list_repo_tree(repo_id=HF_REPO, path=folder_path, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    cdn_urls = [f"{BASE_CDN}/{f}" for f in sorted(files)]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"date": target_date, "files": cdn_urls, "generated_at": datetime.utcnow().isoformat()}, f, indent=2)
    return cdn_urls

def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard backend orchestrator")
    parser.add_argument("--generate-file-list", action="store_true", help="Generate CDN file list for date")
    parser.add_argument("--date", default=_today_str(), help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--serve", action="store_true", help="Start dev FastAPI server")
    args = parser.parse_args()

    if args.generate_file_list:
        out_file = CACHE_DIR / f"file-list-{args.date}.json"
        print(f"Generating file list for {args.date} -> {out_file}")
        try:
            urls = generate_file_list_for_date(args.date, out_file)
            print(f"Wrote {len(urls)} file URLs")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.serve:
        # Import here to avoid requiring FastAPI for cron jobs
        from backend.api import app
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
        return

    parser.print_help()
    sys.exit(1)

if __name__ == "__main__":
    main()
```

### backend/api.py
```python
from fastapi import FastAPI, HTTPException
from pathlib import Path
import json
from datetime import date

VANGUARD_ROOT = Path(__file__).parent.parent
CACHE_DIR = VANGUARD_ROOT / "cache"

app = FastAPI(title="Vanguard Backend")

@app.get("/file-list/{target_date}")
def get_file_list(target_date: str):
    """
    Returns cached CDN file list for date folder.
    Example: /file-list/2026-05-02
    """
    cache_file = CACHE_DIR / f"file-list-{target_date}.json"
    if not cache_file.exists():
        raise HTTPException(status_code=404, detail=f"File list for {target_date} not found. Run orchestrator --generate-file-list --date {target_date}")
    with open(cache_file) as f:
        return json.load(f)

@app.get("/hub/{hub_name}")
def get_hub_insight(hub_name: str):
    """
    Top-hub insight stub (RAG integration point).
    Follows pattern: review most-connected hub before planning.
    """
    # TODO: integrate knowledge-rag pipeline for real insights
    return {
        "hub": hub_name,
        "insight": f"Stub: review most-connected hub '{hub_name}' before planning tasks. (Tag: #knowledge-rag #graph #hub)",
        "tags": ["knowledge-rag", "graph", "hub"]
    }

@app.get("/health")
def health():
    return {"status": "ok"}
```

### Makefile (optional convenience)
```make
.PHONY: filelist serve
SHELL := /bin/bash

filelist:
	cd /opt/axentx/vanguard && python -m backend.orchestrator --generate-file-list --date $$(date -I)

serve:
	cd /opt/axentx/vanguard && python -m backend.orchestrator --serve
```

### Cron hygiene (if used)
Add to crontab:
```
SHELL=/bin/bash
*/30 * * * * cd /opt/axentx/vanguard && python -m backend.orchestrator --generate-file-list --date $(date -I) >> /var/log/vanguard-filelist.log 2>&1
```

## 4. Verification
1. Install deps (once):
   ```bash
   cd /opt/axentx/vanguard
   pip install fastapi uvicorn huggingface_hub
   ```
2. Generate file list:
   ```bash
   python -m backend.orchestrator --generate-file-list --date 2026-05-02
   # Expect cache/file-list-2026-05-02.json with CDN URLs
   ```
3. Start dev server:
   ```bash
   python -m backend.orchestrator --serve
   ```
4. Check endpoints:
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/file-list/2026-05-02
   curl http://localhost:8000/hub/MOC
   ```
5. Confirm CDN URLs are valid (one HEAD request):
   ```bash
   curl -I "$(jq -r '.files[0]' cache/file-list-2026-05-02.json)"
   ```
6. Cron test (optional):
   - Ensure cron uses `SHELL=/bin/bash` and runs without error; check log file for non-zero exit codes.
