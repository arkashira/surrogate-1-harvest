# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value improvement:**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context. No writes; zero side effects; immediate observability value.

---

### File changes (relative to `/opt/axentx/Costinel`)

```
src/
  api/
    v1/
      cost_anomaly/
        top_hub.py          # new endpoint
  services/
    knowledge_rag.py        # extend with top_hub query helper
  models/
    signal_response.py      # new Pydantic response model
tests/
  api/
    v1/
      cost_anomaly/
        test_top_hub.py
```

---

### 1) Add response model

`src/models/signal_response.py`
```python
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class SignalContext(BaseModel):
    hub: str
    hub_score: float
    evidence: List[Dict[str, Any]]
    related_docs: List[Dict[str, Any]]


class CostAnomalySignalResponse(BaseModel):
    signal_id: str
    timestamp: datetime
    severity: str  # "critical" | "high" | "medium" | "low"
    title: str
    description: str
    metric: str
    value: float
    expected_range: Dict[str, float]
    cloud: str
    account_id: str
    region: Optional[str]
    service: Optional[str]
    context: SignalContext
    recommendations: List[str]
```

---

### 2) Extend knowledge-rag service with top-hub query

`src/services/knowledge_rag.py`
```python
from typing import Dict, Any, Optional
from datetime import datetime, timezone


class KnowledgeRAG:
    def __init__(self, graph_client):
        self.graph = graph_client

    def top_hub(self, days: int = 1, hub_label: Optional[str] = None) -> Dict[str, Any]:
        """
        Find the most-connected hub (or specific hub) and strongest cost-anomaly signal.
        Pattern: top-hub doc insight (2026-04-27)
        """
        # 1) Identify top hub by connection strength
        if hub_label:
            hub_node = self.graph.find_node(label="Hub", name=hub_label)
        else:
            hub_node = self.graph.execute_query("""
                MATCH (h:Hub)
                WITH h, size((h)--()) AS connections
                ORDER BY connections DESC
                LIMIT 1
                RETURN h
            """)
            hub_node = hub_node[0] if hub_node else None

        if not hub_node:
            return {}

        # 2) Find strongest cost-anomaly signal linked to this hub today
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        signals = self.graph.execute_query("""
            MATCH (h:Hub {id: $hub_id})-[:INDICATES|ASSOCIATED_WITH|TRIGGERS*1..3]-(s:Signal {category: "cost_anomaly"})
            WHERE s.timestamp >= $today_start
            RETURN s
            ORDER BY s.severity_score DESC
            LIMIT 1
        """, {"hub_id": hub_node["id"], "today_start": today_start.isoformat()})

        if not signals:
            return {}

        signal = signals[0]

        # 3) Gather evidence and related docs
        evidence = self.graph.execute_query("""
            MATCH (s:Signal {id: $signal_id})-[:SUPPORTED_BY]->(e:Evidence)
            RETURN e {.*} AS evidence
        """, {"signal_id": signal["id"]})

        related_docs = self.graph.execute_query("""
            MATCH (s:Signal {id: $signal_id})-[:REFERENCES|MENTIONS]->(d:Document)
            RETURN d {.title, .url, .snippet} AS doc
            ORDER BY d.relevance DESC
            LIMIT 5
        """, {"signal_id": signal["id"]})

        return {
            "hub": hub_node.get("name", hub_node.get("id")),
            "hub_score": hub_node.get("connection_score", 0.0),
            "signal": signal,
            "evidence": evidence,
            "related_docs": related_docs,
        }
```

---

### 3) Add endpoint

`src/api/v1/cost_anomaly/top_hub.py`
```python
from fastapi import APIRouter, HTTPException
from src.services.knowledge_rag import KnowledgeRAG
from src.models.signal_response import CostAnomalySignalResponse
from datetime import datetime
import uuid

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])

# In practice, inject via DI; here we assume a singleton/graph client exists
_knowledge_rag = KnowledgeRAG(graph_client=None)  # replace with actual client


@router.get("/signal/top-hub", response_model=CostAnomalySignalResponse)
async def get_top_hub_signal(hub: str = None):
    """
    Deterministic read-only endpoint:
    Query knowledge graph for today's top hub and strongest cost-anomaly signal.
    Pattern: top-hub doc insight (2026-04-27) + Sense + Signal
    """
    try:
        result = _knowledge_rag.top_hub(days=1, hub_label=hub)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Graph query failed: {exc}") from exc

    if not result or "signal" not in result:
        raise HTTPException(status_code=404, detail="No cost-anomaly signal found for top hub today")

    signal = result["signal"]
    severity_map = {
        4: "critical", 3: "high", 2: "medium", 1: "low"
    }
    severity = severity_map.get(signal.get("severity_level", 1), "low")

    return CostAnomalySignalResponse(
        signal_id=signal.get("id", str(uuid.uuid4())),
        timestamp=signal.get("timestamp", datetime.utcnow()),
        severity=severity,
        title=signal.get("title", "Cost anomaly detected"),
        description=signal.get("description", ""),
        metric=signal.get("metric", "cost"),
        value=float(signal.get("value", 0.0)),
        expected_range=signal.get("expected_range", {"min": 0.0, "max": 0.0}),
        cloud=signal.get("cloud", "unknown"),
        account_id=signal.get("account_id", "unknown"),
        region=signal.get("region"),
        service=signal.get("service"),
        context={
            "hub": result.get("hub", "unknown"),
            "hub_score": float(result.get("hub_score", 0.0)),
            "evidence": result.get("evidence", []),
            "related_docs": result.get("related_docs", []),
        },
        recommendations=signal.get("recommendations", []),
    )
```

---

### 4) Register route

In your main API router (e.g., `src/api/v1/router.py`), add:
```python
from src.api.v1.cost_anomaly.top_hub import router as top_hub_router

api_router.include_router(top_hub_router)
```

---

### 5) Minimal test

`tests/api/v1/cost_anomaly/test_top_hub.py`
```python
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from src.api.v1.cost_anomaly.top_hub import router
from src.services.knowledge_rag import KnowledgeRAG

client = TestClient(router)


def test_top_hub_signal_found(monkeypatch):
    mock_graph = MagicMock()
    mock_graph.find_node.return_value = {"id": "hub-moc", "name": "MOC", "connection_score": 98.2}
    mock_graph.execute_query.side_effect = [
        [{"id": "sig-123", "title": "Spike in MOC compute", "severity_level": 4, "severity_score": 9.8,
          "category": "cost_anomaly", "timestamp": "2026-04-27T12
