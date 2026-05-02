# Costinel / quality

## Implementation Plan — Costinel Quality Increment (<2h)

**Goal:** Add a deterministic, read-only endpoint that surfaces today’s top hub from the knowledge graph as a cost-anomaly signal (Sense + Signal; no Execute).

### Scope (MVP, <2h)
- Add `GET /api/v1/cost-anomaly/signal/top-hub`
  - Returns JSON: `{ "hub": "MOC", "score": 0.94, "context": "...", "generated_at": "..." }`
- Lightweight knowledge-graph lookup (fallback to static rules if graph unavailable)
- No mutations; no auth bypass; no external training calls
- Reuse existing FastAPI app structure and logging
- Add unit test + minimal docs

### Implementation Steps

1. **Add route** in `app/api/cost_anomaly.py` (or create if missing)
2. **Add service** `app/services/top_hub_service.py` to encapsulate lookup
3. **Add fallback** using simple heuristics when graph is unavailable
4. **Add unit test** in `tests/api/test_cost_anomaly.py`
5. **Update docs** (README or API docs) with new endpoint

---

### Code Snippets

#### 1. Service: `app/services/top_hub_service.py`
```python
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

# Lightweight fallback when knowledge graph is unavailable
_TOP_HUB_FALLBACK = {
    "hub": "MOC",
    "score": 0.92,
    "context": "Most-connected hub (fallback): MOC shows recurring cost anomalies in compute overcommit and idle resources."
}

def _try_query_knowledge_graph() -> Optional[Dict[str, Any]]:
    """
    Attempt to query the knowledge graph for today's top hub.
    Replace with real graph client when available.
    Returns None if unavailable or fails.
    """
    try:
        # Placeholder: integrate with knowledge-rag / graph client here.
        # Example: return graph_client.top_hub(day=datetime.utcnow().date())
        logger.debug("Knowledge graph client not configured; using fallback.")
        return None
    except Exception as exc:
        logger.warning("Knowledge graph lookup failed: %s", exc)
        return None

def get_top_hub_signal() -> Dict[str, Any]:
    """
    Deterministic top-hub signal for cost-anomaly sense+signal.
    """
    graph_result = _try_query_knowledge_graph()
    if graph_result:
        payload = {
            "hub": graph_result.get("hub", "MOC"),
            "score": float(graph_result.get("score", 0.0)),
            "context": graph_result.get("context", "Top hub from knowledge graph."),
        }
    else:
        payload = dict(_TOP_HUB_FALLBACK)

    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["source"] = "knowledge-graph" if graph_result else "fallback"
    return payload
```

#### 2. API Route: `app/api/cost_anomaly.py`
```python
from fastapi import APIRouter
from app.services.top_hub_service import get_top_hub_signal

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])

@router.get("/signal/top-hub", summary="Top hub signal for cost anomalies")
async def get_top_hub() -> dict:
    """
    Sense + Signal endpoint: returns today's top hub from the knowledge graph
    as a cost-anomaly signal. No execution or mutation is performed.
    """
    return get_top_hub_signal()
```

#### 3. Mount router in main app (if not auto-discovered)
In `app/main.py` (or wherever FastAPI app is created):
```python
from app.api.cost_anomaly import router as cost_anomaly_router

app.include_router(cost_anomaly_router)
```

#### 4. Unit Test: `tests/api/test_cost_anomaly.py`
```python
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_get_top_hub_returns_valid_payload():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert "hub" in data
    assert isinstance(data["hub"], str)
    assert "score" in data
    assert isinstance(data["score"], (int, float))
    assert "context" in data
    assert "generated_at" in data
    assert "source" in data
```

#### 5. Minimal API Docs Update (README snippet)
Add to API section:
```markdown
### Cost Anomaly Signals

- `GET /api/v1/cost-anomaly/signal/top-hub` — Returns today's top hub from the knowledge graph as a cost-anomaly signal (Sense + Signal). Example:
  ```json
  {
    "hub": "MOC",
    "score": 0.94,
    "context": "Most-connected hub: recurring compute overcommit anomalies.",
    "generated_at": "2026-05-03T12:34:56+00:00",
    "source": "knowledge-graph"
  }
  ```
```

---

### Acceptance Criteria
- Endpoint returns 200 and valid JSON.
- Contains `hub`, `score`, `context`, `generated_at`, `source`.
- No side effects (read-only).
- Unit test passes.
- No breaking changes.

### Time Estimate
- Implementation: ~45m
- Tests + docs: ~30m
- Buffer: ~45m

Total <2h.
