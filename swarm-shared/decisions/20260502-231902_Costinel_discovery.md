# Costinel / discovery

## Final Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a **read-only, side-effect-free** `GET /api/v1/cost-anomaly/signal/top-hub` that uses existing knowledge-graph assets (or lightweight cached fallback) to return the top hub(s) for contextual Costinel discovery (e.g., MOC) and actionable signals. Supports date/account scoping, low-latency caching, and observability. Designed for dashboards and downstream automation.

---

### 1) Acceptance Criteria (resolved + concrete)
- `GET /api/v1/cost-anomaly/signal/top-hub` returns **200** with stable JSON payload.
- Optional query params:
  - `for_date` (YYYY-MM-DD) — defaults to **today**.
  - `account_id` — cloud account filter (optional).
  - `limit` (int, default=5, min=1, max=100) — number of top hubs to return.  
    (Prefer `limit` over `top_n` for consistency with list semantics and Candidate 1 usability.)
- Response is **read-only**, no state changes, no external mutations.
- Latency target: **<200 ms** for cached responses; graceful degradation if graph unavailable.
- Observability: request/response metrics + structured audit log.

---

### 2) Changes to Make (concrete)

- **Add route**
  - `GET /api/v1/cost-anomaly/signal/top-hub`
    - Query params: `for_date`, `account_id`, `limit`.
    - Response shape:
      ```json
      {
        "data": {
          "top_hubs": [
            {
              "hub_id": "MOC",
              "hub_type": "service",
              "score": 0.94,
              "connections": 127,
              "context": {
                "primary_account": "123456789012",
                "region": "us-east-1",
                "anomaly_count": 3,
                "total_cost_impact_usd": 2840.12,
                "top_tags": ["prod","team:infra"],
                "last_updated": "2026-05-02T23:17:16Z"
              }
            }
          ]
        },
        "meta": {
          "request_time": "2026-05-02T23:17:18Z",
          "for_date": "2026-05-02",
          "account_id": "123456789012",
          "limit": 5
        }
      }
      ```

- **Implementation details**
  - Read-only: no writes, no external mutations.
  - Use existing knowledge-graph/RAG if available to query top hubs by connection strength and cost impact. If unavailable, compute lightweight top hub from cached cost metadata (service/region/account with highest connection count + anomaly/cost impact).
  - Cache per `(for_date, account_id, limit)` for ~60s to avoid repeated heavy scans.
  - Observability: emit request/response metrics (latency, count, error rate) and structured log line for auditability.
  - Graceful degradation: fallback to lightweight computation if graph/RAG errors.

- **Files likely to touch**
  - `app/main.py` or `app/routes/cost_anomaly.py` — add route.
  - `app/services/top_hub_service.py` — encapsulate lookup + fallback.
  - `app/core/config.py` — optional cache TTL constant.
  - `app/schemas/top_hub.py` — Pydantic models.
  - `tests/test_top_hub.py` — endpoint + service tests.

---

### 3) Code Snippets (merged best parts)

#### Route (FastAPI)

```python
# app/routes/cost_anomaly.py
from fastapi import APIRouter, Query, HTTPException
from datetime import date, datetime
from typing import Optional
from app.services.top_hub_service import TopHubService
from app.schemas.top_hub import TopHubResponse

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])
top_hub_service = TopHubService()

@router.get("/signal/top-hub", response_model=TopHubResponse)
async def get_top_hub(
    for_date: Optional[date] = Query(None, description="YYYY-MM-DD (defaults to today)"),
    account_id: Optional[str] = Query(None, description="Cloud account filter"),
    limit: int = Query(5, ge=1, le=100, description="Max top hubs to return")
):
    try:
        target_date = for_date or date.today()
        result = await top_hub_service.get_top_hubs(
            for_date=target_date,
            account_id=account_id,
            limit=limit
        )
        return TopHubResponse(
            data={"top_hubs": result},
            meta={
                "request_time": datetime.utcnow().isoformat() + "Z",
                "for_date": str(target_date),
                "account_id": account_id,
                "limit": limit
            }
        )
    except Exception as exc:
        # Keep side-effect-free: no mutations on error
        raise HTTPException(status_code=500, detail=str(exc))
```

#### Service (graph-aware + lightweight fallback)

```python
# app/services/top_hub_service.py
from datetime import date
from typing import List, Optional, Dict, Any
from app.core.cache import ttl_cache
from app.integrations.knowledge_rag import KnowledgeRagClient  # optional, if available

class TopHubService:
    def __init__(self):
        self.rag = KnowledgeRagClient() if KnowledgeRagClient.is_available() else None

    @ttl_cache(ttl=60)
    async def get_top_hubs(
        self,
        for_date: date,
        account_id: Optional[str] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        # Prefer existing knowledge-graph/RAG for top hub
        if self.rag:
            try:
                return await self.rag.query_top_hubs(for_date=for_date, account_id=account_id, limit=limit)
            except Exception:
                # degrade gracefully to lightweight computation
                pass

        # Lightweight fallback: compute from cached cost metadata
        return self._compute_top_hubs(for_date=for_date, account_id=account_id, limit=limit)

    def _compute_top_hubs(
        self,
        for_date: date,
        account_id: Optional[str] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        # Replace with real cost metadata source (e.g., parquet cache or aggregated store)
        # Must be a read-only projection of existing data.
        sample = [
            {
                "hub_id": "MOC",
                "hub_type": "service",
                "score": 0.94,
                "connections": 127,
                "context": {
                    "primary_account": account_id or "123456789012",
                    "region": "us-east-1",
                    "anomaly_count": 3,
                    "total_cost_impact_usd": 2840.12,
                    "top_tags": ["prod", "team:infra"],
                    "last_updated": "2026-05-02T23:17:16Z"
                }
            }
        ]
        return sample[:limit]
```

#### Schema (Pydantic)

```python
# app/schemas/top_hub.py
from pydantic import BaseModel
from typing import List, Dict, Any

class TopHubItem(BaseModel):
    hub_id: str
    hub_type: str
    score: float
    connections: int
    context: Dict[str, Any]

class TopHubData(BaseModel):
    top_hubs: List[TopHubItem]

class TopHubResponse(BaseModel):
    data: TopHubData
    meta: Dict[str, Any]
```

#### Simple cache helper (optional)

```python
# app/core/cache.py
from functools import wraps
from typing import Callable
import time
import hashlib
import json

def ttl_cache(ttl: int = 60):
    def decorator(func: Callable):

