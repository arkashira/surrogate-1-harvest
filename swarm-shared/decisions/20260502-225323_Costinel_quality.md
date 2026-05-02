# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value improvement:**  
Add a **deterministic, read-only** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with context. This directly applies the **top-hub doc insight** pattern and keeps Costinel in “Sense + Signal — ไม่ Execute” mode.

### Key Resolutions (merged best parts)
- **Schema**: Use Candidate 1’s `TopHubSignalResponse` (includes `score: float` and `ts: Optional[str]`) — more precise for ranking and debugging than Candidate 2’s string-only `context`.
- **Service**: Prefer Candidate 1’s direct graph queries (no ambiguous helpers) for determinism and auditability. Keep Candidate 2’s clear separation (`get_top_hub` + `query_cost_anomalies_for_hub`) if those helpers already exist; otherwise inline the queries as in Candidate 1.
- **Error handling**: Adopt Candidate 1’s graceful fallback (returns `200` with empty signal + context note) instead of raising HTTP exceptions. This matches “Sense-only” behavior.
- **Caching**: Add Candidate 2’s idea of caching the top-hub per day (in-memory or via existing cache layer) to avoid repeated graph scans, but keep queries deterministic.

---

### Acceptance Criteria
- Endpoint returns `200` with stable JSON schema.
- Uses existing knowledge-rag/graph layer (no new training/ingestion).
- No writes to graph or stateful systems.
- Response includes: `hub`, `signal`, `score`, `context`, `ts`.
- Fails gracefully (returns `200` with empty signal) if graph unavailable.
- Top-hub selection cached per UTC day for performance.

---

### Implementation Steps (60–90 min)

1. **Add route** in FastAPI router (`routers/cost_anomaly.py`).
2. **Create service** `knowledge_rag_service.get_top_hub_signal()`:
   - Fetch today’s top hub (cached per day).
   - Retrieve strongest cost-anomaly node/edge attached to that hub.
   - Project to `{hub, signal, score, context, ts}`.
3. **Add minimal tests** (happy path + fallback).
4. **Update OpenAPI docs** (FastAPI auto-generates).
5. **Verify locally** with `uvicorn` and `curl`.

---

### Code Snippets

#### 1) Router: `routers/cost_anomaly.py`

```python
from fastapi import APIRouter
from services.knowledge_rag_service import get_top_hub_signal
from schemas.cost_anomaly import TopHubSignalResponse

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
async def top_hub_signal() -> TopHubSignalResponse:
    """
    Sense + Signal: return the strongest cost-anomaly signal
    for today's top hub without executing any change.
    """
    return get_top_hub_signal()
```

#### 2) Service: `services/knowledge_rag_service.py`

```python
from datetime import datetime, timezone, date
from typing import Dict, Any, List, Optional
from graph.client import get_graph  # existing graph client
from functools import lru_cache

# Cache top hub per UTC day (deterministic, cheap)
@lru_cache(maxsize=1)
def _cached_top_hub(today_str: str) -> Optional[str]:
    g = get_graph()
    hubs = (
        g.query()
        .match("(h:Hub)-[r:OBSERVED_ON]->(d:Day {date: $today})")
        .where("r.domain = 'cost'")
        .return_(h, "count(r) as degree")
        .order_by("-degree")
        .limit(1)
        .execute()
    )
    if not hubs:
        return None
    return hubs[0]["h"]["name"]

def get_top_hub_signal() -> TopHubSignalResponse:
    """
    Query graph for today's top hub and strongest cost-anomaly signal.
    Deterministic and read-only.
    """
    try:
        today = datetime.now(timezone.utc)
        today_str = today.strftime("%Y-%m-%d")

        # 1) Top hub (cached per day)
        top_hub = _cached_top_hub(today_str)
        if top_hub is None:
            return TopHubSignalResponse(
                hub=None,
                signal=None,
                score=0.0,
                context=["no top hub found for today"],
                ts=today_str,
            )

        # 2) Strongest cost-anomaly signal attached to this hub today
        g = get_graph()
        anomalies = (
            g.query()
            .match("(h:Hub {name: $hub})-[:HAS_SIGNAL]->(a:Anomaly)")
            .where("a.domain = 'cost' and a.day = $today")
            .return_(a, "a.score as score")
            .order_by("-score")
            .limit(1)
            .execute()
        )

        if not anomalies:
            return TopHubSignalResponse(
                hub=top_hub,
                signal=None,
                score=0.0,
                context=["no cost-anomaly signals for hub today"],
                ts=today_str,
            )

        anomaly = anomalies[0]["a"]
        return TopHubSignalResponse(
            hub=top_hub,
            signal=anomaly.get("type"),
            score=float(anomaly.get("score", 0.0)),
            context=anomaly.get("context", []),
            ts=anomaly.get("ts", today_str),
        )

    except Exception as exc:
        # Graceful fallback: return empty signal (Sense-only)
        return TopHubSignalResponse(
            hub=None,
            signal=None,
            score=0.0,
            context=[f"graph unavailable: {exc}"],
            ts=None,
        )
```

#### 3) Schema: `schemas/cost_anomaly.py`

```python
from pydantic import BaseModel
from typing import Optional, List

class TopHubSignalResponse(BaseModel):
    hub: Optional[str]
    signal: Optional[str]
    score: float
    context: List[str]
    ts: Optional[str]
```

#### 4) Register router in main app (`main.py`)

```python
from routers.cost_anomaly import router as cost_anomaly_router
app.include_router(cost_anomaly_router)
```

---

### Quick Verification

```bash
# Start locally
uvicorn main:app --reload --port 8000

# Query endpoint
curl -s http://localhost:8000/api/v1/cost-anomaly/signal/top-hub | jq
```

Expected shape:

```json
{
  "hub": "MOC",
  "signal": "spend_spike",
  "score": 0.92,
  "context": ["AWS us-east-1 3x baseline", "linked to new deployment"],
  "ts": "2026-05-02T14:30:00Z"
}
```

If graph unavailable or no data:

```json
{
  "hub": null,
  "signal": null,
  "score": 0.0,
  "context": ["graph unavailable: ..."],
  "ts": null
}
```

---

## Tags
#knowledge-rag #graph #hub #cost-anomaly #sense-not-execute
