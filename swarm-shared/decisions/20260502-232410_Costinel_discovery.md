# Costinel / discovery

## Final Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a **read-only, side-effect-free** `GET /api/v1/cost-anomaly/signal/top-hub` that surfaces the most-connected hub (e.g., "MOC") with contextual insights from the knowledge-rag graph. No writes, no mutations, no external side effects.

---

### 1) Architecture & Data Flow (read-only)

```
Client
  │
  ▼
FastAPI (Costinel) ──► KnowledgeRAG/Graph (read) ──► Top-hub + insights
  │
  ▼
JSON response { hub, rank, edges, signals, recommendations }
```

- No POST/PUT/DELETE. No background jobs triggered.
- Uses existing RAG/graph index (already built by prior pipelines).
- Deterministic selection: highest degree (or configurable policy) + freshness threshold.

---

### 2) Concrete Implementation Steps

#### A) Add endpoint (FastAPI)

File: `app/api/v1/endpoints/cost_anomaly.py` (create or extend)

```python
from fastapi import APIRouter, HTTPException
from app.services.knowledge_rag import KnowledgeRAGService
from app.schemas.cost_anomaly import TopHubSignalResponse

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])
rag = KnowledgeRAGService()

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
async def get_top_hub_signal() -> TopHubSignalResponse:
    """
    Read-only signal: most-connected hub + contextual insights.
    Side-effect-free. Used for governance review.
    """
    try:
        hub_node = rag.get_top_hub(limit=1)
        if not hub_node:
            raise HTTPException(status_code=404, detail="No hub found in knowledge graph")

        insights = rag.get_context_for_node(hub_node["id"], max_docs=5)
        signals = rag.extract_signals_for_hub(hub_node["id"])

        return TopHubSignalResponse(
            hub=hub_node["id"],
            rank=hub_node.get("degree", 0),
            category=hub_node.get("category", "unknown"),
            last_updated=hub_node.get("updated_at"),
            edges=hub_node.get("edges", []),
            insights=insights,
            signals=signals,
            recommendations=_build_recommendations(hub_node, signals),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

def _build_recommendations(hub_node, signals):
    # Lightweight, deterministic heuristics — no ML inference here.
    recs = []
    if hub_node.get("degree", 0) > 50:
        recs.append("High-connectivity hub: prioritize governance review and policy alignment.")
    if any(s.get("severity") == "high" for s in signals):
        recs.append("High-severity signals detected: escalate to cost governance board.")
    if not recs:
        recs.append("No immediate action required; continue monitoring.")
    return recs
```

#### B) Service layer (thin wrapper over existing RAG)

File: `app/services/knowledge_rag.py` (add methods)

```python
from typing import List, Dict, Any
from app.graph.knowledge_graph import KnowledgeGraph  # existing graph client

class KnowledgeRAGService:
    def __init__(self):
        self.graph = KnowledgeGraph()

    def get_top_hub(self, limit: int = 1) -> List[Dict[str, Any]]:
        # Deterministic: highest degree, then most recent
        query = """
        MATCH (h:Hub)
        RETURN h.id AS id,
               h.category AS category,
               h.updated_at AS updated_at,
               size((h)--()) AS degree
        ORDER BY degree DESC, h.updated_at DESC
        LIMIT $limit
        """
        results = self.graph.run(query, {"limit": limit})
        return [dict(r) for r in results]

    def get_context_for_node(self, node_id: str, max_docs: int = 5) -> List[Dict[str, Any]]:
        query = """
        MATCH (h:Hub {id: $node_id})--(d:Document)
        RETURN d.id AS doc_id,
               d.title AS title,
               d.summary AS summary,
               d.updated_at AS updated_at
        ORDER BY d.updated_at DESC
        LIMIT $max_docs
        """
        results = self.graph.run(query, {"node_id": node_id, "max_docs": max_docs})
        return [dict(r) for r in results]

    def extract_signals_for_hub(self, hub_id: str) -> List[Dict[str, Any]]:
        # Read signals attached to hub (anomalies, cost spikes, etc.)
        query = """
        MATCH (h:Hub {id: $hub_id})--(s:Signal)
        RETURN s.id AS signal_id,
               s.type AS type,
               s.severity AS severity,
               s.description AS description,
               s.detected_at AS detected_at
        ORDER BY s.detected_at DESC
        """
        results = self.graph.run(query, {"hub_id": hub_id})
        return [dict(r) for r in results]
```

#### C) Schema (Pydantic)

File: `app/schemas/cost_anomaly.py`

```python
from pydantic import BaseModel
from typing import List, Optional, Any
from datetime import datetime

class SignalItem(BaseModel):
    signal_id: str
    type: str
    severity: str
    description: str
    detected_at: datetime

class InsightItem(BaseModel):
    doc_id: str
    title: str
    summary: Optional[str] = None
    updated_at: datetime

class TopHubSignalResponse(BaseModel):
    hub: str
    rank: int
    category: str
    last_updated: Optional[datetime] = None
    edges: List[Any] = []
    insights: List[InsightItem] = []
    signals: List[SignalItem] = []
    recommendations: List[str]
```

#### D) Register router

File: `app/api/api_v1.py`

```python
from fastapi import APIRouter
from app.api.v1.endpoints.cost_anomaly import router as cost_anomaly_router

api_router = APIRouter()
api_router.include_router(cost_anomaly_router)
```

---

### 3) Verification (local)

```bash
# Start server (existing command)
uvicorn app.main:app --reload --port 8000

# Test endpoint
curl -s http://localhost:8000/api/v1/cost-anomaly/signal/top-hub | jq
```

Expected shape:
```json
{
  "hub": "MOC",
  "rank": 87,
  "category": "multi-cloud-orchestration",
  "last_updated": "2026-04-27T14:32:00",
  "edges": [...],
  "insights": [...],
  "signals": [...],
  "recommendations": [
    "High-connectivity hub: prioritize governance review and policy alignment."
  ]
}
```

---

### 4) Tags & Compliance

- Tags: `#knowledge-rag #graph #hub #cost-governance`
- Side-effect-free: confirmed (no writes, no jobs, no mutations).
- Reuses existing RAG/graph pipeline (no new infra).
- Deterministic selection aligned with prior patterns (top-hub doc insight).
