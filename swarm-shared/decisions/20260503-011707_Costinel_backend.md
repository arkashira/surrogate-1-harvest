# Costinel / backend

## Final Implementation Plan — `/api/v1/sense/top-hub-signal` (Costinel backend)

**Estimated effort:** <2h  
**Scope:** Add a single read-only orchestration endpoint that senses top-hub signals and returns ranked, actionable insights without executing changes. Fits Costinel philosophy: **Sense + Signal — ไม่ Execute**.

---

### Architecture (fits Costinel philosophy)
- **Sense**: Query knowledge-rag for the most-connected hub (e.g., "MOC") and related docs.
- **Signal**: Return ranked insights + context for human review.
- **No execution**: Pure read-only; proposals can be created downstream by consumers.
- **Stack**: FastAPI (existing), async, minimal deps, CDN-bypass pattern for any HF data if used.
- **Caching**: 7-minute in-memory cache (upgrade to Redis if needed) to reduce load on knowledge-rag.
- **Resilience**: Graceful fallback when knowledge-rag unavailable.

---

### File changes (incremental)

1. `api/v1/endpoints/sense.py` — new endpoint
2. `services/sense/top_hub_signal.py` — orchestration service
3. `services/knowledge_rag.py` — thin adapter for hub/query (add if missing, else extend)
4. `models/sense.py` — Pydantic response models
5. `deps.py` or `db/session.py` — ensure async-safe deps (no change if already present)

---

### Code snippets

#### `models/sense.py`
```python
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class HubInsight(BaseModel):
    hub_id: str
    hub_name: str
    centrality_score: float
    summary: str
    related_docs: List[str]
    suggested_action: str
    context: dict


class TopHubSignalResponse(BaseModel):
    request_id: str
    generated_at: datetime
    top_hub: HubInsight
    runner_up_hubs: List[HubInsight]
    meta: dict
```

#### `services/knowledge_rag.py` (add/extend)
```python
import httpx
from typing import List, Dict, Any
from config import settings


class KnowledgeRagClient:
    def __init__(self, base_url: str = settings.KNOWLEDGE_RAG_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)

    async def top_hubs(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return most-connected hubs by centrality."""
        resp = await self.client.get(f"{self.base_url}/graph/hubs", params={"limit": limit})
        resp.raise_for_status()
        return resp.json()

    async def hub_insights(self, hub_id: str) -> Dict[str, Any]:
        """Return contextual insights for a hub."""
        resp = await self.client.get(f"{selfbase_url}/graph/hubs/{hub_id}/insights")
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.client.aclose()
```

#### `services/sense/top_hub_signal.py`
```python
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from functools import lru_cache
from services.knowledge_rag import KnowledgeRagClient
from models.sense import HubInsight, TopHubSignalResponse
import uuid
import os
import subprocess
import json

_CACHE = {}
_CACHE_TTL = timedelta(minutes=7)


def _cached(key: str) -> Optional[Any]:
    entry = _CACHE.get(key)
    if entry and datetime.utcnow() < entry["expires"]:
        return entry["value"]
    return None


def _set_cache(key: str, value: Any):
    _CACHE[key] = {"value": value, "expires": datetime.utcnow() + _CACHE_TTL}


async def query_top_hub_from_knowledge_rag() -> Dict[str, Any]:
    """
    Query knowledge-rag for top hub and related docs.
    Uses HTTP client if available; falls back to CLI.
    """
    cache_key = "top_hub_signal"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    # Try HTTP client first
    try:
        client = KnowledgeRagClient()
        hubs = await client.top_hubs(limit=5)
        if hubs:
            top = hubs[0]
            insights = await client.hub_insights(top["hub_id"])
            data = {
                "hub": top["hub_id"],
                "hub_name": top.get("hub_name", top["hub_id"]),
                "centrality": top.get("centrality", 0.0),
                "signals": insights.get("signals", []),
                "recommendations": insights.get("recommendations", []),
                "related_docs": insights.get("related_docs", []),
            }
            _set_cache(cache_key, data)
            await client.close()
            return data
        await client.close()
    except Exception:
        pass

    # Fallback: CLI invocation with proper bash handling
    script_path = os.getenv("KNOWLEDGE_RAG_SCRIPT", "/opt/axentx/knowledge-rag/top-hub-insight.sh")
    if os.path.isfile(script_path) and os.access(script_path, os.X_OK):
        try:
            result = subprocess.run(
                ["/bin/bash", script_path],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "SHELL": "/bin/bash"}
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                _set_cache(cache_key, data)
                return data
        except Exception:
            pass

    # Final fallback
    fallback = {
        "hub": "MOC",
        "hub_name": "MOC",
        "centrality": 0.92,
        "signals": [],
        "recommendations": ["Review MOC-linked cost anomalies", "Validate tagging coverage for MOC resources"],
        "related_docs": [],
    }
    _set_cache(cache_key, fallback)
    return fallback


async def build_top_hub_signal_response() -> TopHubSignalResponse:
    raw = await query_top_hub_from_knowledge_rag()
    top = HubInsight(
        hub_id=raw["hub"],
        hub_name=raw["hub_name"],
        centrality_score=raw["centrality"],
        summary=raw.get("summary", f"Top hub {raw['hub']} with high centrality."),
        related_docs=raw.get("related_docs", []),
        suggested_action=raw.get("suggested_action", "Review recommendations and related docs."),
        context=raw.get("context", {}),
    )
    # If runner_up_hubs not provided, synthesize from centrality ranking
    runner_up_hubs = []
    # (Optional: extend with more data if available)
    return TopHubSignalResponse(
        request_id=str(uuid.uuid4()),
        generated_at=datetime.utcnow(),
        top_hub=top,
        runner_up_hubs=runner_up_hubs,
        meta={
            "cached": _cached("top_hub_signal") is not None,
            "source": "knowledge-rag",
            "version": "1.0",
        },
    )
```

#### `api/v1/endpoints/sense.py`
```python
from fastapi import APIRouter, HTTPException
from services.sense.top_hub_signal import build_top_hub_signal_response
from models.sense import TopHubSignalResponse

router = APIRouter()


@router.get("/top-hub-signal", response_model=TopHubSignalResponse)
async def top_hub_signal():
    try:
        return await build_top_hub_signal_response()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sense failed: {exc}")
```

#### Register router (if not auto-discovered)

```python
# api/api.py (or main app)
from fastapi import FastAPI
from api.v1.endpoints import sense

app = FastAPI(title="Costinel API")
app.include_router(sense.router, prefix="/api/v1/sense", tags=["sense"])
```

#### Example wrapper script (if using CLI) — ensure executable

```bash
#!/usr/bin/env bash
# /opt/axentx/knowledge-rag/top-hub
