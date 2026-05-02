# Costinel / discovery

## Final Implementation Plan — `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & Non-goals**  
- Read-only (Sense + Signal). No mutations, no side effects.  
- Optional `?for_date=YYYY-MM-DD` (default today).  
- Reuse existing knowledge-graph assets; avoid recomputing heavy graph analytics per request.  
- Return compact JSON suitable for dashboard widgets and alerting pipelines.  
- Must be deployable quickly (single primary file + tests + route mount).

---

### 1) Architecture (minimal, high-value)

- **Route**: `GET /api/v1/cost-anomaly/signal/top-hub`  
- **Handler**: `get_cost_anomaly_top_hub_signal`  
- **Service**: `TopHubSignalService`  
  - Uses lightweight hub-selection heuristic: highest weighted degree in date-filtered subgraph (fast, explainable).  
  - Projects a `CostAnomalySignal` with:
    - `hub_id`, `hub_label`, `hub_type`
    - `score` (centrality / connection weight)
    - `context_snippets` (top 3 related docs/entities)
    - `for_date`, `generated_at`, `ttl_seconds`
- **Caching**: 5-minute in-memory cache keyed by `for_date` (avoid graph scans on every dashboard poll).  
- **Error handling**:
  - 400 for invalid date.
  - 204 when no hub/signal available (noisy graph).
  - 500 only on unexpected failures.

---

### 2) File changes (single primary file + optional route mount)

- `src/services/top_hub_signal.py` — new service + model.  
- `src/api/v1/endpoints/cost_anomaly.py` — add route (or create if missing).  
- `tests/api/v1/test_cost_anomaly.py` — add tests for the endpoint.

If route file doesn’t exist, create it and mount under `/api/v1` in your router (FastAPI/Flask — adapt snippets to your stack).

---

### 3) Code snippets

#### `src/models/cost_anomaly_signal.py` (if not present)

```python
from datetime import date
from typing import List
from pydantic import BaseModel


class ContextSnippet(BaseModel):
    doc_id: str
    title: str
    snippet: str
    rel_weight: float


class CostAnomalySignal(BaseModel):
    hub_id: str
    hub_label: str
    hub_type: str
    score: float
    context_snippets: List[ContextSnippet]
    for_date: date
    generated_at: str  # ISO timestamp
    ttl_seconds: int = 300
```

---

#### `src/services/top_hub_signal.py`

```python
from datetime import date, datetime
from typing import List, Optional
from functools import lru_cache

from src.models.cost_anomaly_signal import CostAnomalySignal, ContextSnippet
from src.graph.knowledge_graph import KnowledgeGraph  # adapt import to your graph client


class TopHubSignalService:
    """
    Sense + Signal: find the strongest knowledge-graph hub for a given date
    and produce a cost-anomaly signal without side effects.
    """

    def __init__(self, graph: KnowledgeGraph):
        self.graph = graph

    @staticmethod
    def _today_str() -> str:
        return date.today().isoformat()

    def _normalize_date(self, for_date: Optional[str]) -> date:
        if for_date is None:
            return date.today()
        try:
            return date.fromisoformat(for_date)
        except ValueError:
            raise ValueError("Invalid for_date, expected YYYY-MM-DD")

    @lru_cache(maxsize=32)
    def get_top_hub_signal(self, for_date: Optional[str] = None) -> Optional[CostAnomalySignal]:
        """
        Returns the top hub signal for `for_date`.
        Uses lightweight hub/centrality heuristic:
          - highest weighted degree in the date-filtered subgraph.
        """
        target_date = self._normalize_date(for_date)
        iso_date = target_date.isoformat()

        # 1) Fetch candidate hubs for date (lightweight)
        # Assumes graph has node labels like "Hub", "Topic", "MOC" and edges with `weight` and `date`.
        hubs = self.graph.query(
            """
            MATCH (h:Hub)-[r:MENTIONED_ON]->(d:Doc)
            WHERE d.date = $iso_date OR $iso_date IS NULL
            RETURN h.id AS hub_id,
                   h.label AS hub_label,
                   h.type AS hub_type,
                   sum(r.weight) AS total_weight
            ORDER BY total_weight DESC
            LIMIT 1
            """,
            {"iso_date": iso_date},
        )

        if not hubs:
            return None

        top = hubs[0]

        # 2) Fetch related docs/snippets for context (top 3)
        related = self.graph.query(
            """
            MATCH (h:Hub {id: $hub_id})-[r:MENTIONED_ON]->(d:Doc)
            WHERE d.date = $iso_date
            RETURN d.id AS doc_id,
                   d.title AS title,
                   d.snippet AS snippet,
                   r.weight AS rel_weight
            ORDER BY r.weight DESC
            LIMIT 3
            """,
            {"hub_id": top["hub_id"], "iso_date": iso_date},
        )

        snippets = [
            ContextSnippet(
                doc_id=r["doc_id"],
                title=r["title"] or "",
                snippet=r["snippet"] or "",
                rel_weight=float(r["rel_weight"] or 0.0),
            )
            for r in related
        ]

        return CostAnomalySignal(
            hub_id=top["hub_id"],
            hub_label=top["hub_label"],
            hub_type=top["hub_type"] or "Hub",
            score=float(top["total_weight"] or 0.0),
            context_snippets=snippets,
            for_date=target_date,
            generated_at=datetime.utcnow().isoformat() + "Z",
            ttl_seconds=300,
        )
```

---

#### `src/api/v1/endpoints/cost_anomaly.py`

```python
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from src.services.top_hub_signal import TopHubSignalService
from src.graph.knowledge_graph import get_knowledge_graph  # adapt to your DI
from src.models.cost_anomaly_signal import CostAnomalySignal

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


def get_signal_service(graph=Depends(get_knowledge_graph)) -> TopHubSignalService:
    return TopHubSignalService(graph)


@router.get("/signal/top-hub", response_model=CostAnomalySignal)
async def get_cost_anomaly_top_hub_signal(
    for_date: Optional[date] = Query(
        None,
        description="Target date (YYYY-MM-DD). Defaults to today.",
        example="2026-04-27",
    ),
    service: TopHubSignalService = Depends(get_signal_service),
) -> CostAnomalySignal:
    """
    Sense + Signal: Return the strongest knowledge-graph hub as a cost-anomaly signal.
    Read-only. No mutations.
    """
    try:
        iso_date = for_date.isoformat() if for_date else None
        signal = service.get_top_hub_signal(for_date=iso_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if signal is None:
        # 204 via exception or return empty response — choose per API conventions.
        raise HTTPException(status_code=204, detail="No top-hub signal available")

    return signal
```

---

#### `tests/api/v1/test_cost_anomaly.py`

```python
from datetime import date
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.api.v1.endpoints.cost_anomaly import router
from src.graph.knowledge_graph import KnowledgeGraph

client = TestClient(router)


def test_get_top_hub_signal_ok():
    mock_graph = MagicMock(spec=KnowledgeGraph)
    mock_graph.query.side_effect = [
        [
            {
                "hub_id": "MOC",
                "hub_label": "Map of Content",
