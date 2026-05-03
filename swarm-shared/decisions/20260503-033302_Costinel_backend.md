# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

### Goal
Ship a **non-blocking Top-Hub Signal Panel** in Costinel that surfaces the most-connected hub (e.g., "MOC") using **CDN-first data baked at build/orchestration time**. Runtime dashboard makes **zero HF API calls** during request handling or frontend render.

---

### Architecture (CDN-First, Zero Runtime HF Calls)
```
Mac orchestrator (HF API) ──► list_repo_tree (once) ──► top_hubs.json
                                      │
                                      ▼
                        baked into build artifact (static/data/)
                                      │
                                      ▼
                 Costinel backend serves CDN URLs only
                 (FastAPI → static JSON, no /api/ HF calls)
                                      │
                                      ▼
                 Frontend renders Top-Hub Signal Panel
                 (async, non-render-blocking, cache-first)
```

---

### Concrete Steps (120 min)

1. **Create top-hub extractor** (15 min)  
   - Script: `scripts/extract-top-hubs.py` (Python, uses HF API)  
   - Uses `list_repo_tree(path, recursive=False)` per date folder  
   - Computes hub degree from link references in markdown/json  
   - Outputs deterministic `top_hubs.json` with:  
     `{slug, title, degree, cdn_url, updated_at}`  
   - Tie-break: max degree → most recent path

2. **Bake into build** (10 min)  
   - Copy `top_hubs.json` → `backend/static/data/top_hubs.json` during CI/build  
   - Include fallback file in repo for offline dev and CI cache hits

3. **Backend endpoint** (20 min)  
   - FastAPI route: `GET /api/signals/top-hub`  
   - Serves baked JSON with:  
     `Cache-Control: public, max-age=3600, stale-while-revalidate=600`  
   - Non-blocking behavior: returns 204 if file missing (panel hides gracefully)  
   - **Zero HF API calls at runtime**

4. **CDN-first asset links** (10 min)  
   - All hub references use:  
     `https://huggingface.co/datasets/.../resolve/main/...`  
   - No Authorization header required (bypasses 429)

5. **Frontend panel** (30 min)  
   - React component: `TopHubSignalPanel`  
   - Fetches `/api/signals/top-hub` on mount (async, non-render-blocking)  
   - Skeleton loader → render card with hub title, degree, link  
   - Auto-retry with exponential backoff (max 3)  
   - localStorage cache for offline use

6. **Resilience & caching** (15 min)  
   - Backend: stale-while-revalidate (serve stale for 10m while revalidating)  
   - Frontend: cache-first strategy with localStorage fallback  
   - Studio reuse check: skip rebuild if Lightning studio running

7. **Tests & deploy** (20 min)  
   - Unit test: extractor produces valid JSON schema  
   - Integration test: endpoint returns 200 with cached data  
   - Smoke test: panel renders in dev mode  
   - Commit with tags `#knowledge-rag #graph #hub`

---

### Resolved Contradictions (Correctness + Actionability)
- **Extractor language**: Use Python (Candidate 1) because HF Hub libraries and path handling are simpler for this task; avoids extra Node dependency in a Python-first repo unless CI already uses Node heavily.
- **Build artifact location**: Use `backend/static/data/top_hubs.json` (Candidate 1) — keeps CDN-first serving simple and avoids extra routing.
- **Runtime HF calls**: Explicitly forbidden (both candidates agree); enforce by baking only and using CDN URLs.
- **Graceful failure**: Return 204 with empty body when file missing (Candidate 1) — simpler and more cache-friendly than error payloads.
- **Caching strategy**: Combine `max-age=3600` + `stale-while-revalidate=600` (Candidate 1) — balances freshness and resilience.

---

### Code Snippets

#### 1. Extract Top-Hub (scripts/extract-top-hubs.py)
```python
#!/usr/bin/env python3
"""
Extract most-connected hub from knowledge-rag graph.
Uses HF list_repo_tree (non-recursive) + CDN URLs only.
Run from Mac orchestrator after HF API window clears.
"""
import json, os, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_KG_REPO", "axentx/knowledge-rag")
OUTPUT = sys.argv[1] if len(sys.argv) > 1 else "top_hubs.json"

def extract_top_hub():
    api = HfApi()
    # Single call per date folder (non-recursive)
    folders = api.list_repo_tree(repo_id=HF_REPO, path="", recursive=False)
    date_folders = [f for f in folders if f.path.startswith("202") and f.type == "directory"]

    best = None
    for df in sorted(date_folders, key=lambda x: x.path, reverse=True)[:7]:  # last 7 days
        tree = api.list_repo_tree(repo_id=HF_REPO, path=df.path, recursive=False)
        files = [t for t in tree if t.path.endswith((".md", ".json"))]
        for f in files:
            try:
                content = api.hf_hub_download(repo_id=HF_REPO, filename=f.path, repo_type="dataset")
                # Simplified: parse links, count degrees
                with open(content) as fh:
                    text = fh.read()
                degree = text.count("[[") + text.count("http")  # proxy for connectivity
                cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f.path}"
                candidate = {
                    "slug": f.path.replace("/", "-").replace(".md", ""),
                    "title": f.path.split("/")[-1].replace(".md", "").replace("-", " ").title(),
                    "degree": degree,
                    "cdn_url": cdn_url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if best is None or degree > best["degree"] or (degree == best["degree"] and f.path > best["slug"]):
                    best = candidate
            except Exception as e:
                print(f"skip {f.path}: {e}", file=sys.stderr)
                continue

    result = best or {"slug": "moc", "title": "MOC", "degree": 0, "cdn_url": "", "updated_at": datetime.now(timezone.utc).isoformat()}
    with open(OUTPUT, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"✓ wrote {OUTPUT}")

if __name__ == "__main__":
    extract_top_hub()
```

#### 2. FastAPI Endpoint (backend/routes/signals.py)
```python
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import json
from pathlib import Path

router = APIRouter(prefix="/signals", tags=["signals"])

TOP_HUB_PATH = Path(__file__).parent.parent / "static" / "data" / "top_hubs.json"

@router.get("/top-hub")
async def get_top_hub():
    """
    CDN-first top-hub signal.
    Zero HF API calls at runtime.
    """
    try:
        if not TOP_HUB_PATH.exists():
            return JSONResponse(status_code=204, content={})
        with open(TOP_HUB_PATH) as fh:
            data = json.load(fh)
        return JSONResponse(
            content=data,
            headers={
                "Cache-Control": "public, max-age=3600, stale-while-revalidate=600",
                "CDN-Cache-Control": "max-age=86400",
            },
        )
    except Exception:
        # Non-blocking: fail gracefully
        return JSONResponse(status_code=204, content={})
```

#### 3. React Panel (frontend/components/TopHubSignalPanel.tsx)
```tsx
import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import
