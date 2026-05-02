# Costinel / backend

## Final Implementation Plan — Costinel Top-Hub Signal (Backend)

**Scope:** Highest-value, read-only signal endpoint (<2h) that surfaces the most-connected hub (e.g., "MOC") with contextual cost anomalies and recommendations.  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

---

### 1. Architecture (backend)

- **Framework:** FastAPI (async, typed)
- **Data sources:**
  - Primary: PostgreSQL read-replica (or materialized view) for cost events, anomalies, recommendations
  - Accelerator: Redis for cached hub-link graph (fast connected-hub computation)
- **Caching:** 45–60s TTL for full endpoint response; short LRU fallback in-process for hot-path protection
- **Auth:** Bearer token (existing middleware)
- **Observability:** tracing + structured logging (reuse existing middleware)
- **Response shape:**
  - `hub_id`, `hub_name`, `hub_type`
  - `top_signals[]` (anomalies + recommendations)
  - `context` (related accounts, services, regions, time window)
  - `generated_at`, `ttl`

---

### 2. Implementation steps (ordered)

1. Add route + handler (`GET /api/v1/cost-anomaly/signal/top-hub`)
2. Implement `TopHubSignalService`:
   - `get_most_connected_hub()` — uses Redis sorted set of hub-link counts (fast) with Postgres fallback
   - `build_context(hub_id)` — accounts, services, regions, time range
   - `fetch_signals(hub_id)` — recent anomalies + actionable recommendations (limit, sorted by recency/severity)
3. Add layered caching:
   - Endpoint-level: 45–60s TTL via `aiocache` (Redis) or FastAPI `Depends` wrapper
   - Hot-path: lightweight in-process LRU for repeated calls within TTL
4. Add Pydantic response model (`TopHubSignalResponse`) + OpenAPI docs
5. Add minimal unit tests (200 shape, caching behavior stub)
6. Wire into existing middleware (auth, tracing, logging)

---

### 3. Code snippets

#### `main.py` (route registration)

```python
from fastapi import APIRouter
from axentx.costinel.api.v1.endpoints.top_hub import router as top_hub_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(top_hub_router, prefix="/cost-anomaly", tags=["cost-anomaly"])
```

#### `axentx/costinel/api/v1/endpoints/top_hub.py`

```python
from fastapi import APIRouter, Depends
from axentx.costinel.api.v1.schemas.top_hub import TopHubSignalResponse
from axentx.costinel.services.top_hub_signal import TopHubSignalService
from axentx.shared.cache import fast_cache

router = APIRouter()

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
async def get_top_hub_signal(
    service: TopHubSignalService = Depends(),
) -> TopHubSignalResponse:
    return await fast_cache.cached_call(
        key="top_hub_signal",
        ttl=45,
        fn=service.build_top_hub_signal,
    )
```

#### `axentx/costinel/services/top_hub_signal.py`

```python
from datetime import datetime, timedelta, timezone
from typing import List
from axentx.costinel.repositories.hub_repo import HubRepository
from axentx.costinel.repositories.anomaly_repo import AnomalyRepository
from axentx.costinel.repositories.recommendation_repo import RecommendationRepository
from axentx.costinel.api.v1.schemas.top_hub import TopHubSignalResponse, SignalItem, HubContext

class TopHubSignalService:
    def __init__(
        self,
        hub_repo: HubRepository = HubRepository(),
        anomaly_repo: AnomalyRepository = AnomalyRepository(),
        recommendation_repo: RecommendationRepository = RecommendationRepository(),
    ):
        self.hub_repo = hub_repo
        self.anomaly_repo = anomaly_repo
        self.recommendation_repo = recommendation_repo

    async def build_top_hub_signal(self) -> TopHubSignalResponse:
        hub = await self.hub_repo.get_most_connected_hub()
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=7)

        anomalies = await self.anomaly_repo.find_by_hub(
            hub_id=hub.id,
            since=window_start,
            limit=10,
        )
        recommendations = await self.recommendation_repo.find_actionable_by_hub(
            hub_id=hub.id,
            since=window_start,
            limit=10,
        )

        signals: List[SignalItem] = []
        signals.extend([SignalItem.from_anomaly(a) for a in anomalies])
        signals.extend([SignalItem.from_recommendation(r) for r in recommendations])
        signals.sort(key=lambda s: s.detected_at, reverse=True)

        context = HubContext(
            hub_id=hub.id,
            hub_name=hub.name,
            hub_type=hub.type,
            related_accounts=hub.related_accounts,
            services=hub.top_services(limit=5),
            regions=hub.top_regions(limit=5),
            time_window=f"{window_start.isoformat()}Z/{now.isoformat()}Z",
        )

        return TopHubSignalResponse(
            hub_id=hub.id,
            hub_name=hub.name,
            hub_type=hub.type,
            top_signals=signals[:10],
            context=context,
            generated_at=now,
            ttl=60,
        )
```

#### `axentx/costinel/api/v1/schemas/top_hub.py`

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import List

class SignalItem(BaseModel):
    id: str
    type: str  # "anomaly" | "recommendation"
    title: str
    description: str
    severity: str  # "low" | "medium" | "high" | "critical"
    detected_at: datetime
    metadata: dict = Field(default_factory=dict)

    @classmethod
    def from_anomaly(cls, anomaly):
        return cls(
            id=f"anom-{anomaly.id}",
            type="anomaly",
            title=anomaly.title,
            description=anomaly.description,
            severity=anomaly.severity,
            detected_at=anomaly.detected_at,
            metadata={"service": anomaly.service, "delta": anomaly.delta},
        )

    @classmethod
    def from_recommendation(cls, rec):
        return cls(
            id=f"rec-{rec.id}",
            type="recommendation",
            title=rec.title,
            description=rec.description,
            severity=rec.priority,
            detected_at=rec.created_at,
            metadata={"action": rec.action, "estimated_savings": rec.estimated_savings},
        )

class HubContext(BaseModel):
    hub_id: str
    hub_name: str
    hub_type: str
    related_accounts: List[str]
    services: List[str]
    regions: List[str]
    time_window: str

class TopHubSignalResponse(BaseModel):
    hub_id: str
    hub_name: str
    hub_type: str
    top_signals: List[SignalItem]
    context: HubContext
    generated_at: datetime
    ttl: int  # seconds
```

#### Lightweight cache helper (`axentx/shared/cache.py`)

```python
import time
from typing import Callable, Any

class FastCache:
    def __init__(self):
        self._store = {}

    def cached_call(self, key: str, ttl: int, fn: Callable[..., Any]) -> Any:
        now = time.time()
        entry = self._store.get(key)
        if entry and (now - entry["ts"]) < ttl:
            return entry["value"]
        value = fn()
        self._store[key] = {"value": value, "ts": now}
        return value

fast_cache = FastCache()
```

---

### 4. Tests (minimal)

`tests/api/v1/test_top_hub.py`

```python
from fastapi.testclient import TestClient
from axentx.main
