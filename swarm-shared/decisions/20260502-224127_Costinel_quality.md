# Costinel / quality

## Final Synthesized Implementation (Best of Both Candidates)

**Chosen approach:** Add a **deterministic, read-only** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for the strongest cost‑anomaly signal (highest‑centrality hub) and returns a concise, contextual insight.  
**Why this wins:**  
- Candidate 1’s dedicated endpoint is cleaner (separation of concerns) and safer (no schema churn on existing routes).  
- Candidate 2 correctly emphasizes centrality and fast fallback; we adopt that logic inside the service.  
- Both agree on read‑only, <2h, no migrations, and using the existing graph.  
- We resolve contradictions by favoring **correctness + concrete actionability**: deterministic UTC day, explicit fallback (204), stable schema, and minimal surface.

---

## 1) Implementation Plan (≤90 min)

1. **Add new endpoint**  
   `GET /api/v1/cost-anomaly/signal/top-hub`  
   Returns `SignalResponse` (deterministic for UTC day; read‑only).

2. **Implement service method**  
   `KnowledgeRAGService.get_top_hub_signal(tags, date)`  
   - Query graph for hub with highest degree/centrality that has today’s cost‑anomaly signals.  
   - Pick top signal by `score` within that hub.  
   - Project context (neighbors/tags/summary).  
   - Graceful `None` fallback.

3. **Schema**  
   Reuse/define `SignalResponse` (Pydantic). No changes to existing endpoint schemas.

4. **Register route**  
   Include router under `/api/v1/cost-anomaly` with existing auth middleware.

5. **Tests & checks**  
   - Unit test for service query logic (mock graph).  
   - Integration test for endpoint contract and 204 fallback.  
   - Smoke test: ensure no writes and timing <100 ms on small graph.

---

## 2) Code Snippets

### 2.1 Route (FastAPI)

```python
# costinel/api/routes/cost_anomaly.py
from fastapi import APIRouter, Depends, Response
from costinel.services.knowledge_rag import KnowledgeRAGService
from costinel.schemas.signal import SignalResponse
from datetime import datetime, timezone

router = APIRouter()

@router.get("/signal/top-hub", response_model=SignalResponse, status_code=200)
async def get_top_hub_signal(
    rag: KnowledgeRAGService = Depends(),
):
    """
    Deterministic read-only endpoint returning the strongest cost‑anomaly
    signal for today (UTC) from the top hub in the knowledge graph.
    """
    today = datetime.now(timezone.utc).date()
    signal = await rag.get_top_hub_signal(tags=["cost-anomaly"], date=today)
    if not signal:
        return Response(status_code=204)
    return signal
```

### 2.2 KnowledgeRAG Service

```python
# costinel/services/knowledge_rag.py
from __future__ import annotations

from typing import List, Optional, Dict, Any
from datetime import date, datetime, timezone
from costinel.schemas.signal import SignalResponse

class KnowledgeRAGService:
    def __init__(self, graph_client):
        self.graph = graph_client

    async def get_top_hub_signal(self, tags: List[str], date: date) -> Optional[SignalResponse]:
        """
        Query the graph for the highest‑centrality hub with signals for `date`
        and `tags`, then return the top signal with context.

        Deterministic: stable sort -> (degree DESC, score DESC, id ASC).
        Read‑only: no writes.
        """
        # Adapt query to your graph backend (Neo4j shown).
        query = """
        MATCH (h:Hub)-[:HAS_SIGNAL]->(s:Signal)
        WHERE s.date = $date AND ANY(tag IN $tags WHERE tag IN s.tags)
        WITH h, s, size((h)-[:HAS_SIGNAL]-()) as degree
        ORDER BY degree DESC, s.score DESC, s.id ASC
        LIMIT 1
        OPTIONAL MATCH (s)-[:HAS_CONTEXT]->(c:Context)
        RETURN
          h.name       as hub,
          s.id         as signalId,
          s.title      as title,
          s.description as description,
          s.score      as score,
          s.createdAt  as createdAt,
          collect(c.text) as context
        """
        result = await self.graph.run(query, {"date": date.isoformat(), "tags": tags})
        record = result.one()
        if not record:
            return None

        return SignalResponse(
            signalId=record["signalId"],
            title=record["title"],
            description=record["description"],
            hub=record["hub"],
            score=record["score"] or 0.0,
            context=record["context"] or [],
            createdAt=record["createdAt"] or datetime.now(timezone.utc).isoformat(),
        )
```

### 2.3 Schema

```python
# costinel/schemas/signal.py
from pydantic import BaseModel
from typing import List, Optional

class SignalResponse(BaseModel):
    signalId: Optional[str]
    title: str
    description: str
    hub: Optional[str]
    score: float
    context: List[str]
    createdAt: str
```

### 2.4 Route Registration

```python
# costinel/api/__init__.py  (or main.py)
from costinel.api.routes.cost_anomaly import router as cost_anomaly_router
app.include_router(cost_anomaly_router, prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])
```

---

## 3) Quick Validation Steps

1. Start local server.
2. (Optional) Seed graph with a test hub + signal for today.
3. `curl -i http://localhost:8000/api/v1/cost-anomaly/signal/top-hub`
   - Expect `200` with `SignalResponse` when available.
   - Expect `204` when no signal exists.
4. Confirm no writes in logs.
5. Check query timing <100 ms on small graph.

---

## 4) Notes & Risks

- Uses existing graph client; prefer read replicas if available.
- If graph backend differs (Neo4j, NetworkX, etc.), adapt query while keeping method signature stable.
- No cron/background jobs required; deterministic on‑demand read.
- Aligns with pattern “top‑hub insight” and tags `#knowledge-rag #graph #hub`.
