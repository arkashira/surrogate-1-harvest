# Costinel / discovery

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Goal:** Add a deterministic, read-only endpoint `GET /api/v1/cost-anomaly/signal/top-hub` that surfaces today’s top hub from the knowledge graph as a cost-anomaly signal (Sense + Signal; no Execute).

---

### Why this is the highest-value <2h increment
- Directly applies the **top-hub doc insight** pattern (review most-connected hub before planning).
- Fits the **business research + knowledge-rag** pipeline pattern (query top hub and related docs for contextual insights).
- Requires **no infra changes, no training pipeline, no secrets** — pure read-only query + signal.
- Delivers immediate operational value: surfaces the strongest context for today’s cost anomalies without touching execution paths.
- Read-only, deterministic, and scoped to a single endpoint — safe to ship in <2h.

---

### Implementation Steps (≤2h)

1. **Add route**  
   Register `GET /api/v1/cost-anomaly/signal/top-hub` in the router (FastAPI assumed from project style).

2. **Create service layer**  
   Implement `CostAnomalySignalService.get_top_hub_signal()` that:
   - Queries the knowledge graph for today’s top hub (most-connected node).
   - Falls back to a lightweight heuristic if graph unavailable (e.g., most frequent tag/label in today’s signals).
   - Returns enriched context: hub identity, degree/strength, related docs/context, and timestamp.

3. **Response schema**  
   Define `TopHubSignalResponse` with:
   - `signal: "top-hub"`
   - `date: str` (YYYY-MM-DD)
   - `hub: HubInfo`
   - `strength: float`
   - `related_docs: list[DocRef]`
   - `generated_at: datetime`

4. **Wire into DI/container**  
   Ensure service is injectable and uses existing graph client or RAG retriever.

5. **Add tests**  
   One unit test for the service (mock graph) and one integration test for the endpoint.

6. **Update docs**  
   Add endpoint to API docs (OpenAPI) and a short note in README under API section.

---

### Code Snippets

#### 1. Route (FastAPI)
```python
# app/api/v1/endpoints/cost_anomaly.py
from fastapi import APIRouter, Depends
from app.schemas.cost_anomaly import TopHubSignalResponse
from app.services.cost_anomaly_signal import CostAnomalySignalService

router = APIRouter()

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
def get_top_hub_signal(
    service: CostAnomalySignalService = Depends(),
) -> TopHubSignalResponse:
    """
    Sense + Signal: return today's top hub from the knowledge graph
    as a cost-anomaly signal. Read-only. No execution.
    """
    return service.get_top_hub_signal()
```

#### 2. Service
```python
# app/services/cost_anomaly_signal.py
from datetime import datetime, timezone, date
from typing import List, Dict, Any, Optional
from app.core.knowledge_rag import KnowledgeRAGClient
from app.schemas.cost_anomaly import TopHubSignalResponse, HubInfo, DocRef

class CostAnomalySignalService:
    def __init__(self, graph_client: KnowledgeRAGClient = None):
        self.graph = graph_client or KnowledgeRAGClient()

    def get_top_hub_signal(self) -> TopHubSignalResponse:
        today_str = date.today().isoformat()

        # Query top hub (most-connected today)
        top_hub = self.graph.top_hub(
            days=1,
            min_degree=1,
            include_context=True
        )

        # Fallback if graph unavailable
        if not top_hub:
            top_hub = self._fallback_top_hub()

        hub_info = HubInfo(
            id=top_hub["name"],
            label=top_hub.get("label", top_hub["name"]),
            type="hub",
            tags=top_hub.get("tags", [])
        )

        return TopHubSignalResponse(
            signal="top-hub",
            date=today_str,
            hub=hub_info,
            strength=float(top_hub.get("degree", 0)),
            related_docs=[DocRef(**doc) for doc in top_hub.get("related_docs", [])],
            generated_at=datetime.now(timezone.utc),
        )

    def _fallback_top_hub(self) -> Dict[str, Any]:
        # Lightweight heuristic: most frequent label in today's signals
        return {
            "name": "MOC",
            "label": "MOC",
            "degree": 1,
            "tags": ["moc", "cost", "anomaly"],
            "related_docs": [
                {"title": "MOC Overview", "uri": "kb://moc/overview", "relevance": 0.9}
            ],
        }
```

#### 3. Schema
```python
# app/schemas/cost_anomaly.py
from datetime import datetime
from typing import List
from pydantic import BaseModel

class DocRef(BaseModel):
    title: str
    uri: str
    relevance: float

class HubInfo(BaseModel):
    id: str
    label: str
    type: str = "hub"
    tags: List[str] = []

class TopHubSignalResponse(BaseModel):
    signal: str = "top-hub"
    date: str  # YYYY-MM-DD
    hub: HubInfo
    strength: float
    related_docs: List[DocRef]
    generated_at: datetime
```

#### 4. Knowledge client stub (use existing implementation)
```python
# app/core/knowledge_rag.py
from typing import Dict, Any, Optional

class KnowledgeRAGClient:
    def top_hub(self, days: int = 1, min_degree: int = 1, include_context: bool = False) -> Optional[Dict[str, Any]]:
        """
        Return the most-connected hub node for the last `days` days.
        Expected shape:
        {
          "name": "MOC",
          "label": "MOC",
          "degree": 42,
          "tags": ["moc", "cost", "anomaly"],
          "related_docs": [{"title": "...", "uri": "...", "relevance": 0.95}, ...]
        }
        """
        # TODO: integrate with real graph/RAG backend
        # Placeholder for production integration
        return {
            "name": "MOC",
            "label": "MOC",
            "degree": 42,
            "tags": ["moc", "cost", "anomaly"],
            "related_docs": [
                {"title": "MOC Best Practices", "uri": "kb://moc/best-practices", "relevance": 0.92},
                {"title": "Cost Anomaly Patterns", "uri": "kb://cost/anomalies", "relevance": 0.87},
            ],
        }
```

#### 5. Register router (if not auto-discovered)
```python
# app/api/api_v1.py
from fastapi import APIRouter
from app.api.v1.endpoints.cost_anomaly import router as cost_anomaly_router

api_router = APIRouter()
api_router.include_router(cost_anomaly_router, prefix="/cost-anomaly", tags=["cost-anomaly"])
```

#### 6. Tests (minimal)
```python
# tests/test_cost_anomaly_signal.py
from unittest.mock import MagicMock
from app.services.cost_anomaly_signal import CostAnomalySignalService

def test_get_top_hub_signal():
    mock_graph = MagicMock()
    mock_graph.top_hub.return_value = {
        "name": "MOC",
        "label": "MOC",
        "degree": 42,
        "tags": ["moc", "cost", "anomaly"],
        "related_docs": [{"title": "MOC Best Practices", "uri": "kb://moc/best-practices", "relevance": 0.92}],
    }
    service = CostAnomalySignalService(graph_client=mock_graph)
    resp = service.get_top_hub_signal()
    assert resp.hub.id == "MOC"
    assert resp.strength == 42.0
    assert resp.signal == "top-hub"
    assert len(resp.related_docs) == 1

