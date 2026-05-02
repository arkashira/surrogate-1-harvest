# Costinel / discovery

## Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose `GET /api/v1/cost-anomaly/signal/top-hub` that reads the existing knowledge-graph assets, identifies the most-connected hub (e.g., "MOC"), and returns a concise, actionable signal payload keyed by `for_date`. This is read-only, zero side-effects, and directly supports the "Sense + Signal" philosophy.

**Why this now:**  
- Reuses existing `KnowledgeGraph` assets (no recomputation).  
- Aligns with pattern: review most-connected hub before planning.  
- Enables downstream dashboards/alerts to consume top-hub context immediately.

---

### 1) Add route + handler (FastAPI)

File: `/opt/axentx/Costinel/app/api/v1/endpoints/cost_anomaly.py`

```python
from fastapi import APIRouter, Depends, Query
from datetime import date, datetime, timezone
from typing import Optional
from app.services.knowledge_graph import KnowledgeGraph
from app.schemas.cost_anomaly import TopHubSignalResponse

router = APIRouter()

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
def get_top_hub_signal(
    for_date: Optional[date] = Query(None, description="UTC date (YYYY-MM-DD); defaults to today"),
    kg: KnowledgeGraph = Depends(lambda: KnowledgeGraph())
) -> TopHubSignalResponse:
    """
    Sense + Signal: return the most-connected hub and top contextual insights
    for the requested date without any mutations.
    """
    target_date = for_date or _today_utc()
    target_date_str = target_date.isoformat()

    # Reuse existing graph assets; avoid recomputing centrality each call.
    top_hub = kg.top_hub(for_date=target_date_str)
    related_docs = kg.related_docs(node=top_hub["id"], limit=5, for_date=target_date_str)

    return TopHubSignalResponse(
        for_date=target_date_str,
        top_hub=top_hub,
        related_docs=related_docs,
        generated_at=datetime.now(timezone.utc).isoformat(),
        note="Sense + Signal — no execution performed"
    )
```

---

### 2) Schema: response model

File: `/opt/axentx/Costinel/app/schemas/cost_anomaly.py`

```python
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class HubNode(BaseModel):
    id: str
    label: str
    type: str
    centrality: float = Field(..., description="Normalized centrality score")
    metadata: Dict[str, Any] = Field(default_factory=dict)

class RelatedDoc(BaseModel):
    id: str
    title: str
    source: str
    relevance: float
    uri: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class TopHubSignalResponse(BaseModel):
    for_date: str
    generated_at: str
    top_hub: HubNode
    related_docs: List[RelatedDoc]
    note: str
```

---

### 3) KnowledgeGraph utilities (minimal additions)

File: `/opt/axentx/Costinel/app/services/knowledge_graph.py`

```python
from typing import Dict, Any, List, Optional
from datetime import date

class KnowledgeGraph:
    def __init__(self, graph_path: Optional[str] = None):
        # Assume existing loader; keep read-only.
        self.graph_path = graph_path or "/opt/axentx/Costinel/data/knowledge_graph"
        self._graph = self._load_graph()

    def _load_graph(self):
        # Placeholder: existing loader logic (e.g., networkx/rdflib)
        # Must not mutate or recompute heavy analytics here.
        return {}

    def top_hub(self, for_date: str) -> Dict[str, Any]:
        """
        Return most-connected hub for date.
        Prefer precomputed centrality if available; otherwise compute lightweight degree.
        """
        # Example stub — replace with real asset lookup by date.
        # Pattern: review most-connected hub (e.g., "MOC")
        return {
            "id": "MOC",
            "label": "Mission Operating Center",
            "type": "hub",
            "centrality": 0.92,
            "metadata": {"for_date": for_date, "source": "knowledge_rag"}
        }

    def related_docs(self, node: str, limit: int = 5, for_date: str = "") -> List[Dict[str, Any]]:
        """
        Return top related docs for node, optionally scoped by date.
        """
        # Example stub — replace with real graph neighborhood lookup.
        return [
            {
                "id": f"doc-{i}",
                "title": f"Insight doc {i} for {node}",
                "source": "knowledge_rag",
                "relevance": round(0.95 - i*0.1, 2),
                "uri": f"/docs/{node}/{i}",
                "metadata": {"for_date": for_date}
            }
            for i in range(1, limit + 1)
        ]
```

---

### 4) Register router

File: `/opt/axentx/Costinel/app/api/v1/api.py`

```python
from fastapi import APIRouter
from app.api.v1.endpoints.cost_anomaly import router as cost_anomaly_router

api_router = APIRouter()
api_router.include_router(cost_anomaly_router, prefix="/cost-anomaly", tags=["cost-anomaly"])
```

---

### 5) Add route to main app (if not auto-included)

File: `/opt/axentx/Costinel/main.py`

```python
from fastapi import FastAPI
from app.api.v1.api import api_router

app = FastAPI(title="Costinel")
app.include_router(api_router, prefix="/api/v1")
```

---

### 6) Quick test (local)

```bash
# Start server (uvicorn)
uvicorn main:app --host 0.0.0.0 --port 8000

# Query endpoint
curl "http://localhost:8000/api/v1/cost-anomaly/signal/top-hub?for_date=2026-05-02"
```

Expected response shape:
```json
{
  "for_date": "2026-05-02",
  "generated_at": "2026-05-02T14:23:00+00:00",
  "top_hub": {
    "id": "MOC",
    "label": "Mission Operating Center",
    "type": "hub",
    "centrality": 0.92,
    "metadata": { "for_date": "2026-05-02", "source": "knowledge_rag" }
  },
  "related_docs": [
    { "id": "doc-1", "title": "Insight doc 1 for MOC", "source": "knowledge_rag", "relevance": 0.85, "uri": "/docs/MOC/1", "metadata": { "for_date": "2026-05-02" } }
  ],
  "note": "Sense + Signal — no execution performed"
}
```

---

**Tags:** #knowledge-rag #graph #hub #cost-anomaly #api #sense-signal
