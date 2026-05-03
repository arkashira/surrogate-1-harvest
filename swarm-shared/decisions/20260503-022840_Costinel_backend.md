# Costinel / backend

## Highest-Value Incremental Improvement (<2h)

**Goal**: Ship a **read-only Signal Panel** (top-hub insights) backed by a **lightweight CDN-first backend proxy** that:
- Avoids HF API rate limits by using CDN URLs (`resolve/main/`)
- Pre-lists file paths once and embeds them in the training/data-loading script
- Uses a minimal FastAPI proxy for resilience (caching, retries, circuit-breaker)
- Exposes a single `/signals/top-hub` endpoint returning contextual insights for the most-connected hub (e.g., "MOC")

**Why this now**:
- Aligns with #knowledge-rag #graph #hub patterns
- Matches the CDN-bypass insight (HF CDN has higher limits; no auth required)
- Fits Costinel philosophy: *Sense + Signal — ไม่ Execute*
- Deliverable is frontend-ready and backend-safe (read-only)

---

## Implementation Plan (≤2h)

1. **Add backend proxy module** (`costinel/proxy/signal_proxy.py`)
   - FastAPI router: `/signals/top-hub`
   - Uses `httpx.AsyncClient` with retries + timeout
   - Caches responses (in-memory, 60s TTL) to reduce CDN bursts
   - Falls back to last-known-good payload on CDN failure

2. **Add config for CDN sources** (`costinel/config/signal_sources.py`)
   - Map hub → HF dataset repo + folder path
   - Example: `MOC -> axentx/knowledge-rag/signals/top-hub/moc/`

3. **Add utility to generate static file list** (`scripts/generate_signal_filelist.py`)
   - Runs on Mac (or CI) using HF API *once* per update window
   - Calls `list_repo_tree(path, recursive=False)` for a date folder
   - Saves `signal_filelist.json` into repo (committed or artifact)
   - Training/data loader will use this list for CDN-only fetches (zero API calls during training)

4. **Add lightweight data loader** (`costinel/data/signal_loader.py`)
   - Reads `signal_filelist.json`
   - Builds CDN URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`
   - Downloads + parses only `{prompt,response}` fields (project at parse time)
   - Returns top insights for requested hub

5. **Wire into main app** (`costinel/main.py`)
   - Include router: `app.include_router(signal_router, prefix="/api")`

6. **Add tests** (`tests/test_signal_proxy.py`)
   - Mock CDN responses; verify caching and fallback behavior

7. **Update Dockerfile** (if needed)
   - Ensure `httpx` and `fastapi` are installed (already likely present)

---

## Code Snippets

### 1. Config: signal_sources.py
```python
# costinel/config/signal_sources.py
from dataclasses import dataclass

@dataclass
class SignalSource:
    repo: str
    folder: str  # e.g. "signals/top-hub/moc/2026-05-03"

SIGNAL_SOURCES = {
    "MOC": SignalSource(
        repo="axentx/knowledge-rag",
        folder="signals/top-hub/moc/2026-05-03",
    ),
}
```

### 2. Proxy: signal_proxy.py
```python
# costinel/proxy/signal_proxy.py
import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from functools import lru_cache

from costinel.config.signal_sources import SIGNAL_SOURCES

router = APIRouter()
_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)

# Simple in-memory cache: (hub) -> (expires_at, payload)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 60.0  # seconds

async def _fetch_cdn(url: str) -> dict[str, Any]:
    try:
        resp = await _client.get(url)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise RuntimeError(f"CDN fetch failed: {exc}") from exc

def _cached(hub: str) -> dict[str, Any] | None:
    entry = _CACHE.get(hub)
    if entry and time.time() < entry[0]:
        return entry[1]
    return None

def _set_cache(hub: str, payload: dict[str, Any]) -> None:
    _CACHE[hub] = (time.time() + _CACHE_TTL, payload)

@router.get("/signals/top-hub")
async def get_top_hub_signals(hub: str = "MOC") -> dict[str, Any]:
    """
    Returns contextual insights for the top hub (e.g., MOC).
    Uses CDN-first strategy; falls back to last-known-good payload.
    """
    if hub not in SIGNAL_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown hub: {hub}")

    cached = _cached(hub)
    if cached:
        return {"hub": hub, "source": "cache", "data": cached}

    source = SIGNAL_SOURCES[hub]
    # Expect filelist to exist alongside this code (committed or artifact)
    filelist_path = f"signal_filelist_{hub.lower()}.json"
    try:
        # Load precomputed file list (CDN-only paths)
        import json
        from pathlib import Path

        p = Path(filelist_path)
        if not p.is_file():
            raise FileNotFoundError(f"Missing filelist: {filelist_path}")

        with p.open() as f:
            files = json.load(f)  # list of relative paths in repo folder

        # Build CDN URLs and fetch first N files (limit for responsiveness)
        insights = []
        limit = 5
        for rel in files[:limit]:
            cdn_url = f"https://huggingface.co/datasets/{source.repo}/resolve/main/{source.folder}/{rel}"
            try:
                payload = await _fetch_cdn(cdn_url)
                # Project to {prompt, response} only
                insights.append({
                    "prompt": payload.get("prompt"),
                    "response": payload.get("response"),
                })
            except Exception:
                continue  # skip individual failures

        result = {"hub": hub, "insights": insights, "count": len(insights)}
        _set_cache(hub, result)
        return {"hub": hub, "source": "cdn", "data": result}
    except Exception as exc:
        # Fallback to last cached value if available
        fallback = _CACHE.get(hub)
        if fallback:
            return {"hub": hub, "source": "fallback", "data": fallback[1]}
        raise HTTPException(status_code=502, detail=f"Unable to fetch signals: {exc}") from exc
```

### 3. Utility: generate_signal_filelist.py (run on Mac/CI)
```python
# scripts/generate_signal_filelist.py
import json
import os
import sys
from huggingface_hub import HfApi

HF_TOKEN = os.getenv("HF_TOKEN")  # optional; public repos may not require it
api = HfApi(token=HF_TOKEN)

REPO = "axentx/knowledge-rag"
FOLDER = "signals/top-hub/moc/2026-05-03"  # parameterized per hub/date
OUTFILE = "signal_filelist_moc.json"

def main() -> None:
    try:
        tree = api.list_repo_tree(repo_id=REPO, path=FOLDER, recursive=False)
        files = [item.rfilename for item in tree if item.rfilename.endswith((".json", ".jsonl", ".parquet"))]
        with open(OUTFILE, "w") as f:
            json.dump(files, f, indent=2)
        print(f"Wrote {len(files)} files to {OUTFILE}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### 4. Data loader (CDN-only, for training pipeline)
```python
# costinel/data/signal_loader.py
import json
from pathlib import Path
from typing import Iterator, Dict, Any
import httpx

CDN_TEMPLATE = "https://huggingface.co
