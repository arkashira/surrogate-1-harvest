# Costinel / backend

## Implementation Plan (≤2h)

**Goal**: Add a Top-Hub Signal Panel to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **zero Hugging Face API calls during render**, fully CDN-backed.

### Scope (backend + minimal frontend contract)
- Backend: FastAPI endpoint `/api/v1/top-hub/signal` returning CDN-backed hub + proposals.
- Data source: Single pre-listed JSON file from HF repo folder (e.g., `top-hub/moc/signals-2026-05-03.json`) downloaded via CDN (`resolve/main/...`) at startup + optional refresh job.
- Caching: In-memory cache with 5-minute TTL; no API calls during request.
- Frontend: Consume endpoint and render a card (existing dashboard slot).

### Steps (≤2h)
1. Add config for HF dataset repo + folder path (env or constant).
2. Add service to fetch file list once (Mac/Orchestrator) and embed path or fetch via CDN at startup.
3. Add FastAPI endpoint with in-memory cache (5m TTL).
4. Add background refresh task (optional) to re-fetch CDN file periodically.
5. Minimal Pydantic models and error handling.
6. Small frontend integration (if applicable) — otherwise document contract.

---

## Code Snippets

### 1) Config / constants

```python
# costinel/config.py
import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # HF dataset repo (public)
    HF_DATASET_REPO: str = "AXENTX/Costinel-signals"
    # Folder containing per-hub signal JSON files
    HF_SIGNALS_PATH: str = "top-hub"
    # Default hub to show
    DEFAULT_HUB: str = "MOC"
    # CDN base (no auth)
    HF_CDN_BASE: str = "https://huggingface.co/datasets"
    # Refresh interval seconds (for background job)
    SIGNAL_REFRESH_SECONDS: int = 300

    class Config:
        env_file = ".env"

settings = Settings()
```

---

### 2) CDN-backed loader (no HF API during requests)

```python
# costinel/services/top_hub.py
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx
from fastapi import BackgroundTasks

from costinel.config import settings

logger = logging.getLogger("costinel.top_hub")

# In-memory cache entry
_CACHE: Dict[str, Any] = {"data": None, "ts": None}

def _cdn_url(repo: str, path: str) -> str:
    return f"{settings.HF_CDN_BASE}/{repo}/resolve/main/{path}"

async def _fetch_json_via_cdn(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("CDN fetch failed %s: %s", url, exc)
        return None

async def refresh_signals_cache(force: bool = False) -> bool:
    """
    Refresh in-memory cache from CDN.
    File expected: top-hub/{hub}/signals-{date}.json
    Fallback to default hub if specific file missing.
    """
    hub = settings.DEFAULT_HUB
    # Try date-stamped file first, then hub.json, then hub folder index
    candidates = [
        f"{settings.HF_SIGNALS_PATH}/{hub}/signals-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json",
        f"{settings.HF_SIGNALS_PATH}/{hub}/signals.json",
        f"{settings.HF_SIGNALS_PATH}/{hub}.json",
    ]

    data = None
    for path in candidates:
        url = _cdn_url(settings.HF_DATASET_REPO, path)
        payload = await _fetch_json_via_cdn(url)
        if payload:
            data = payload
            logger.info("Loaded signals from CDN: %s", path)
            break

    if data is None:
        logger.warning("No signals file found for hub=%s", hub)
        return False

    _CACHE["data"] = data
    _CACHE["ts"] = datetime.now(timezone.utc).isoformat()
    return True

async def get_cached_signals() -> Optional[Dict[str, Any]]:
    """Return cached payload; None if not loaded."""
    return _CACHE["data"]

async def warmup_signals(background: BackgroundTasks) -> None:
    """Warm cache on startup; schedule periodic refresh."""
    await refresh_signals_cache()
    # schedule periodic refresh
    background.add_task(_periodic_refresh)

async def _periodic_refresh() -> None:
    import asyncio
    while True:
        await asyncio.sleep(settings.SIGNAL_REFRESH_SECONDS)
        try:
            await refresh_signals_cache()
        except Exception as exc:
            logger.error("Periodic refresh failed: %s", exc)
```

---

### 3) Pydantic models and endpoint

```python
# costinel/api/v1/top_hub.py
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from costinel.services import top_hub as hub_service

router = APIRouter(prefix="/api/v1/top-hub", tags=["top-hub"])

class Proposal(BaseModel):
    id: str
    title: str
    description: str
    impact_usd_monthly: Optional[float] = None
    confidence: Optional[float] = None
    category: Optional[str] = None
    action_url: Optional[str] = None

class HubSignal(BaseModel):
    hub: str
    name: str
    description: Optional[str] = None
    updated_at: Optional[datetime] = None
    proposals: List[Proposal]

class TopHubSignalResponse(BaseModel):
    hub_signal: HubSignal
    generated_at: datetime
    cache_ts: Optional[str] = None

@router.get("/signal", response_model=TopHubSignalResponse)
async def get_top_hub_signal() -> TopHubSignalResponse:
    payload = await hub_service.get_cached_signals()
    if payload is None:
        # Try one quick refresh (best-effort) before failing
        ok = await hub_service.refresh_signals_cache()
        if not ok:
            raise HTTPException(status_code=503, detail="Signals unavailable")
        payload = await hub_service.get_cached_signals()
        if payload is None:
            raise HTTPException(status_code=503, detail="Signals unavailable")

    hub = payload.get("hub", "MOC")
    name = payload.get("name", hub)
    description = payload.get("description")
    updated_at = payload.get("updated_at")
    raw_proposals = payload.get("proposals", [])
    proposals = [Proposal(**p) for p in raw_proposals[:3]]

    hub_signal = HubSignal(
        hub=hub,
        name=name,
        description=description,
        updated_at=updated_at,
        proposals=proposals,
    )

    return TopHubSignalResponse(
        hub_signal=hub_signal,
        generated_at=datetime.now(timezone.utc),
        cache_ts=_CACHE.get("ts"),
    )
```

---

### 4) Startup wiring

```python
# costinel/main.py (excerpt)
from fastapi import FastAPI
from fastapi.background import BackgroundTasks

from costinel.api.v1 import top_hub as top_hub_router
from costinel.services import top_hub as hub_service

app = FastAPI(title="Costinel")

@app.on_event("startup")
async def startup_event():
    background = BackgroundTasks()
    await hub_service.warmup_signals(background)
    # Note: BackgroundTasks added tasks will run in the event loop.
    # If you prefer a dedicated periodic scheduler (e.g., APScheduler), swap in.

app.include_router(top_hub_router.router)
```

---

### 5) Example CDN file format (for repo `AXENTX/Costinel-signals`)

Path: `top-hub/moc/signals-2026-05-03.json`

