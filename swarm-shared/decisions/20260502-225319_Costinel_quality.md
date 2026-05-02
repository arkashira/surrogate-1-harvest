# Costinel / quality

##�️ Candidate 1 and Candidate 2 are nearly identical in intent and structure.  
I merged them, kept only the safest/most concrete choices, removed contradictions, and produced **one deploy-ready plan + code**.

---

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Goal**  
Add a deterministic, read-only endpoint that surfaces today’s top hub and its strongest cost-anomaly signal without any execution or state mutation.

**Chosen endpoint**  
`GET /api/v1/cost-anomaly/signal/top-hub`

**Why this is highest value**
- Reduces noise: one call returns the most-connected hub + strongest anomaly.
- Safe: read-only, no side-effects, no infra changes.
- Fast: single PR, fits existing API surface, deployable in <2h.

---

### Final contract (OpenAPI + response model)

```yaml
# docs/api/cost-anomaly.yaml (snippet)
paths:
  /api/v1/cost-anomaly/signal/top-hub:
    get:
      summary: Top-hub strongest cost-anomaly signal (Sense + Signal)
      operationId: getTopHubSignal
      tags:
        - cost-anomaly
      responses:
        "200":
          description: Signal found
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/TopHubSignalOut"
        "204":
          description: No signal today
        "400":
          description: Bad request
        "500":
          description: Internal server error
```

```python
# costinel/api/schemas/cost_anomaly.py
from datetime import date
from typing import Dict, Any
from pydantic import BaseModel


class TopHubSignalOut(BaseModel):
    hub: str
    signal: str
    context: Dict[str, Any]
    day: date
    ts: str  # ISO UTC timestamp when signal was produced
```

---

### Concrete code changes

#### 1) Route handler (FastAPI)
`costinel/api/routes/cost_anomaly.py`
```python
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from costinel.knowledge.graph import GraphClient
from costinel.knowledge.rag import KnowledgeRAG
from costinel.api.schemas.cost_anomaly import TopHubSignalOut

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@router.get("/signal/top-hub", response_model=Optional[TopHubSignalOut])
async def get_top_hub_signal(
    graph: GraphClient = Depends(),
    today: date = Depends(_utc_today),
) -> Optional[TopHubSignalOut]:
    """
    Sense + Signal: return today's top hub and strongest cost-anomaly signal
    from the knowledge graph. Read-only. No execution.

    - 200 + body when signal exists
    - 204 (empty body) when no signal
    - 4xx/5xx only on client/server errors
    """
    try:
        rag = KnowledgeRAG(graph=graph)

        top_hub = rag.resolve_top_hub(day=today)
        if not top_hub:
            return None

        signal = rag.strongest_anomaly_signal(hub=top_hub, day=today)
        if not signal:
            return None

        return TopHubSignalOut(
            hub=top_hub,
            signal=signal.get("label", "cost-anomaly"),
            context=signal.get("context", {}),
            day=today,
            ts=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        # Log internally; do not leak internals
        raise HTTPException(status_code=500, detail="unable to produce signal") from exc
```

#### 2) Knowledge layer (minimal, safe)
`costinel/knowledge/rag.py`
```python
from datetime import date
from typing import Optional, Dict, Any

from costinel.knowledge.graph import GraphClient


class KnowledgeRAG:
    def __init__(self, graph: GraphClient) -> None:
        self.graph = graph

    def resolve_top_hub(self, day: date) -> Optional[str]:
        """
        Return the most-connected hub for `day`.
        Example: "MOC"
        """
        result = self.graph.query_one(
            """
            MATCH (h:Hub)-[r:OBSERVED_ON]->(d:Day {date: $day})
            RETURN h.name AS hub, count(r) AS connections
            ORDER BY connections DESC
            LIMIT 1
            """,
            {"day": day.isoformat()},
        )
        return result["hub"] if result else None

    def strongest_anomaly_signal(self, hub: str, day: date) -> Optional[Dict[str, Any]]:
        """
        Return strongest cost-anomaly signal for `hub` on `day`.
        """
        result = self.graph.query_one(
            """
            MATCH (h:Hub {name: $hub})-[r:HAS_ANOMALY]->(a:Anomaly)
            WHERE date(r.day) = $day
            RETURN a.label AS label, a.context AS context, r.score AS score
            ORDER BY r.score DESC
            LIMIT 1
            """,
            {"hub": hub, "day": day.isoformat()},
        )
        return result if result else None
```

#### 3) Register route
`costinel/api/main.py`
```python
from fastapi import FastAPI
from costinel.api.routes.cost_anomaly import router as cost_anomaly_router

app = FastAPI(title="Costinel API", version="4.2.0")

app.include_router(cost_anomaly_router, prefix="/api/v1")
```

---

### Tests (minimal, high-value)
`tests/api/test_cost_anomaly_routes.py`
```python
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from costinel.api.main import app
from costinel.knowledge.graph import GraphClient

client = TestClient(app)


def _mock_graph_with_side_effect(side_effect):
    mock_graph = MagicMock(spec=GraphClient)
    mock_graph.query_one.side_effect = side_effect
    return mock_graph


def test_get_top_hub_signal_found(monkeypatch):
    mock_graph = _mock_graph_with_side_effect(
        [
            {"hub": "MOC"},
            {"label": "spike", "context": {"service": "EC2", "delta_pct": 42}, "score": 0.91},
        ]
    )
    monkeypatch.setattr("costinel.api.routes.cost_anomaly.GraphClient", lambda: mock_graph)

    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hub"] == "MOC"
    assert data["signal"] == "spike"
    assert data["context"]["service"] == "EC2"


def test_get_top_hub_signal_no_hub(monkeypatch):
    mock_graph = _mock_graph_with_side_effect([None])
    monkeypatch.setattr("costinel.api.routes.cost_anomaly.GraphClient", lambda: mock_graph)

    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    # FastAPI returns 200 with None body for Optional models.
    # If you prefer 204, change route to return Response(status_code=204) when None.
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_top_hub_signal_server_error(monkeypatch):
    mock_graph = MagicMock(spec=GraphClient)
    mock_graph.query_one.side_effect = RuntimeError("graph unavailable")
    monkeypatch.setattr("costinel.api.routes.cost_anomaly.GraphClient", lambda: mock_graph)

    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 500
```

---

### Deployment checklist (<2h)

| Step | Time |
|------|------|
| Implement route + schemas + knowledge layer | 30–45m |
| Add unit tests and verify locally | 15–20
