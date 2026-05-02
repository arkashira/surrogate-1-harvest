# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value improvement:**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context. No writes, no side effects. Expose via backend stub.

### Why this is highest value
- Directly applies the **top-hub doc insight** pattern (review most-connected hub before planning).
- Uses existing knowledge-rag/graph infrastructure — no new infra.
- Read-only, zero side effects — safe to ship.
- Complements Costinel’s “Sense + Signal — ไม่ Execute” philosophy.
- Can be built and tested end-to-end in <2h.
- Provides immediate, actionable signal.

---

### Implementation Steps (≤2h)

1. **Add backend route** (`backend/app/api/v1/cost_anomaly.py`)
   - `GET /api/v1/cost-anomaly/signal/top-hub`
   - Query knowledge graph for today’s top hub (e.g., via `knowledge_rag.get_top_hub(date=today)`).
   - Return strongest cost-anomaly signal with context:
     - hub name / id
     - signal type / severity
     - affected resources
     - timestamp
     - short rationale

2. **Add service layer** (`backend/app/services/cost_anomaly_service.py`)
   - `get_top_hub_cost_anomaly_signal()` — orchestrates:
     - `knowledge_rag.get_top_hub()`
     - `knowledge_rag.query_signals(hub=..., type="cost-anomaly", date=today)`
     - picks strongest signal (by severity/score)
     - formats response

3. **Add minimal knowledge-rag adapter** (if not present)
   - Stub/mock for now if real graph isn’t wired; make it deterministic so tests pass.
   - Later replace with real `knowledge_rag` calls.

4. **Add tests** (`tests/api/test_cost_anomaly_top_hub_signal.py`)
   - Test 200 response shape.
   - Test deterministic output for a given date.

5. **Update docs** (`docs/api.md` or OpenAPI spec)
   - Add endpoint description, example request/response.

6. **Verify locally**
   - Start backend.
   - `curl http://localhost:8000/api/v1/cost-anomaly/signal/top-hub`
   - Confirm JSON response with top-hub signal.

---

### Code Snippets

#### 1. Route (FastAPI example)

`backend/app/api/v1/cost_anomaly.py`
```python
from fastapi import APIRouter, Depends
from app.services.cost_anomaly_service import get_top_hub_cost_anomaly_signal
from app.schemas.cost_anomaly import TopHubCostAnomalySignalResponse
from datetime import date

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

@router.get("/signal/top-hub", response_model=TopHubCostAnomalySignalResponse)
def read_top_hub_cost_anomaly_signal(today: date = Depends(lambda: date.today())):
    """
    Deterministic, read-only endpoint.
    Returns the strongest cost-anomaly signal for today's top hub.
    No writes, no side effects.
    """
    signal = get_top_hub_cost_anomaly_signal(today)
    return signal
```

#### 2. Service

`backend/app/services/cost_anomaly_service.py`
```python
from datetime import date
from app.lib.knowledge_rag import get_top_hub, query_signals
from app.schemas.cost_anomaly import TopHubCostAnomalySignalResponse, SignalContext

def get_top_hub_cost_anomaly_signal(today: date) -> TopHubCostAnomalySignalResponse:
    # 1) Get today's top hub
    top_hub = get_top_hub(date=today)  # returns hub name/id

    # 2) Query cost-anomaly signals for that hub
    signals = query_signals(
        hub=top_hub,
        signal_type="cost-anomaly",
        date=today
    )

    # 3) Pick strongest signal (by severity/score)
    if not signals:
        # Deterministic fallback when no signals
        return TopHubCostAnomalySignalResponse(
            hub=top_hub,
            signal_type="cost-anomaly",
            severity="none",
            score=0.0,
            affected_resources=[],
            timestamp=today.isoformat(),
            rationale="No cost-anomaly signals detected for top hub today.",
            context=SignalContext(
                top_hub=top_hub,
                query_date=today.isoformat(),
                note="Read-only signal endpoint"
            )
        )

    strongest = max(signals, key=lambda s: s.get("score", 0))

    return TopHubCostAnomalySignalResponse(
        hub=top_hub,
        signal_type="cost-anomaly",
        severity=strongest.get("severity", "medium"),
        score=strongest.get("score", 0.0),
        affected_resources=strongest.get("affected_resources", []),
        timestamp=strongest.get("timestamp", today.isoformat()),
        rationale=strongest.get("rationale", "Cost anomaly detected."),
        context=SignalContext(
            top_hub=top_hub,
            query_date=today.isoformat(),
            note="Read-only signal endpoint"
        )
    )
```

#### 3. Schemas

`backend/app/schemas/cost_anomaly.py`
```python
from pydantic import BaseModel
from datetime import date
from typing import List, Optional

class SignalContext(BaseModel):
    top_hub: str
    query_date: str
    note: str = "Read-only signal endpoint"

class TopHubCostAnomalySignalResponse(BaseModel):
    hub: str
    signal_type: str
    severity: str
    score: float
    affected_resources: List[str]
    timestamp: str
    rationale: str
    context: SignalContext
```

#### 4. Knowledge-rag stub (if needed)

`backend/app/lib/knowledge_rag.py`
```python
from datetime import date
from typing import List, Dict

def get_top_hub(today: date) -> str:
    """
    Deterministic stub: return a fixed top hub for reproducibility.
    Replace with real graph query in production.
    """
    # Example: return most-connected hub for today
    return "MOC"

def query_signals(hub: str, signal_type: str, today: date) -> List[Dict]:
    """
    Deterministic stub: return sample cost-anomaly signals for the hub.
    """
    # Simulate strongest signal for MOC
    if hub == "MOC" and signal_type == "cost-anomaly":
        return [
            {
                "severity": "high",
                "score": 0.92,
                "affected_resources": ["prod-cluster-01", "data-warehouse-east"],
                "timestamp": today.isoformat(),
                "rationale": "Unusual spend spike in compute and storage for top hub MOC."
            }
        ]
    return []
```

#### 5. Test

`tests/api/test_cost_anomaly_top_hub_signal.py`
```python
from fastapi.testclient import TestClient
from app.main import app
from datetime import date

client = TestClient(app)

def test_get_top_hub_cost_anomaly_signal():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert "hub" in data
    assert "signal_type" in data
    assert "severity" in data
    assert "score" in data
    assert "context" in data
    assert data["context"]["note"] == "Read-only signal endpoint"
```

#### 6. Register router

Ensure router is included in `backend/app/main.py`:
```python
from app.api.v1.cost_anomaly import router as cost_anomaly_router

app.include_router(cost_anomaly_router)
```

---

### Verification

```bash
# Start backend (adjust command to your setup)
uvicorn app.main:app --reload --port 8000

# Test endpoint
curl http://localhost:8000/api/v1/cost-anomaly/signal/top-hub
```

Expected JSON:
```json
{
  "hub": "MOC",
  "signal_type": "cost-anomaly",
 
