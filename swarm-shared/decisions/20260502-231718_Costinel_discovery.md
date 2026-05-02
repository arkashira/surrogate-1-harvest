# Costinel / discovery

## Final Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a read-only, side-effect-free `GET /api/v1/cost-anomaly/signal/top-hub` that uses existing knowledge-graph assets to return the top hub(s) for the requested date (default today UTC). This enables ops and anomaly workflows to quickly surface the most-connected hub (e.g., "MOC") and contextual insights without touching production state.

### Acceptance Criteria (resolved)
- **Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`
- **Query params:**  
  - `for_date` (optional): `YYYY-MM-DD`. Default = today UTC.  
  - `top_n` (optional): integer 1–10. Default = 1. Clamp to range; reject non-integer.
- **Response (200):**  
  ```json
  {
    "requested_at": "2026-05-02T14:23:00Z",
    "for_date": "2026-05-02",
    "top_n": 3,
    "hubs": [
      {
        "hub_id": "MOC",
        "label": "MOC",
        "centrality_score": 0.92,
        "related_docs": [
          { "doc_id": "doc-123", "title": "Cost Anomaly Runbook", "score": 0.87 }
        ]
      }
    ]
  }
  ```
- **Behavior:**  
  - Read-only, no side effects, no mutations, no external calls that mutate state.  
  - Graceful degradation: if graph data unavailable for date, return `200` with empty `hubs: []`.  
  - Input validation: invalid date → `400`; `top_n` out of range → clamp to 1–10.  
  - Use existing knowledge-graph assets (Neo4j/NetworkX/precomputed index).  
  - No authentication/authorization changes required for MVP.

---

### Implementation Steps (60–90 min)

1. **Add route and handler** in the API layer (FastAPI).  
2. **Implement lightweight service** that queries the knowledge graph for top hubs by centrality for the target date and maps hub → related docs (top-k by edge weight/similarity).  
3. **Add minimal unit tests** for validation and success path.  
4. **Verify no side effects** (no writes; idempotent reads).  
5. **Mount endpoint** and confirm integration.

---

### Code Snippets

#### 1) Route + handler (FastAPI)
File: `app/api/v1/endpoints/cost_anomaly.py`

```python
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.services.knowledge.graph_service import graph_service, NotFoundError

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


class RelatedDoc(BaseModel):
    doc_id: str
    title: str
    score: float = Field(ge=0.0)


class HubResult(BaseModel):
    hub_id: str
    label: str
    centrality_score: float
    related_docs: List[RelatedDoc] = []


class TopHubResponse(BaseModel):
    requested_at: datetime
    for_date: str  # YYYY-MM-DD
    top_n: int
    hubs: List[HubResult]


class TopHubQuery(BaseModel):
    for_date: str | None = None
    top_n: int = Query(1, ge=1, le=10)

    @field_validator("for_date", mode="before")
    @classmethod
    def default_today_utc(cls, v):
        if v is None:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return v

    @field_validator("for_date")
    @classmethod
    def valid_iso_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("for_date must be YYYY-MM-DD")
        return v

    @field_validator("top_n")
    @classmethod
    def clamp_top_n(cls, v):
        return max(1, min(10, int(v)))


@router.get("/signal/top-hub", response_model=TopHubResponse)
async def get_top_hub(q: TopHubQuery):
    try:
        hubs = graph_service.top_hubs(for_date=q.for_date, top_n=q.top_n)
        result = TopHubResponse(
            requested_at=datetime.now(timezone.utc),
            for_date=q.for_date,
            top_n=q.top_n,
            hubs=[
                HubResult(
                    hub_id=h["hub_id"],
                    label=h["label"],
                    centrality_score=h["centrality_score"],
                    related_docs=[
                        RelatedDoc(doc_id=d["doc_id"], title=d["title"], score=d["score"])
                        for d in h.get("related_docs", [])
                    ],
                )
                for h in hubs
            ],
        )
        return result
    except NotFoundError:
        # Graceful degradation: no graph data for date -> empty list
        return TopHubResponse(
            requested_at=datetime.now(timezone.utc),
            for_date=q.for_date,
            top_n=q.top_n,
            hubs=[],
        )
    except Exception as exc:
        # Log internally; return 500 for unexpected errors
        raise HTTPException(status_code=500, detail="Internal error while fetching top hubs") from exc
```

#### 2) Knowledge-graph service (reuse existing assets)
File: `app/services/knowledge/graph_service.py`

```python
from datetime import datetime
from typing import List, Dict, Any

from app.core.config import settings


class NotFoundError(Exception):
    pass


class GraphService:
    """
    Lightweight adapter over existing knowledge-graph store.
    Replace internals with real calls (Neo4j / NetworkX / precomputed index).
    """

    def __init__(self):
        # Example: self.driver = GraphDatabase.driver(settings.KG_URI, ...)
        pass

    def top_hubs(self, for_date: str, top_n: int) -> List[Dict[str, Any]]:
        """
        Return top hubs by centrality for `for_date`.

        Expected return shape:
        [
          {
            "hub_id": "MOC",
            "label": "MOC",
            "centrality_score": 0.92,
            "related_docs": [
              {"doc_id": "doc-123", "title": "Cost Anomaly Runbook", "score": 0.87},
              ...
            ]
          },
          ...
        ]

        Integration notes:
        - If using Neo4j:
          MATCH (h:Hub)-[r:RELATED_TO]->(d:Doc)
          WHERE r.for_date = $for_date
          RETURN h.id AS hub_id, h.label AS label, r.centrality AS centrality_score,
                 collect({doc_id: d.id, title: d.title, score: r.doc_score})[..$top_k] AS related_docs
          ORDER BY centrality_score DESC LIMIT $top_n

        - If using precomputed files:
          Load `knowledge/top_hubs/{for_date}.json` and return top_n entries.

        - If no data for date: raise NotFoundError or return [].
        """
        # Placeholder: integrate with existing graph store or precomputed index.
        # For MVP, graceful no-op:
        return []


graph_service = GraphService()
```

#### 3) Minimal unit tests
File: `tests/api/v1/test_cost_anomaly_top_hub.py`

```python
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app

client = TestClient(app)


def test_get_top_hub_defaults():
    with patch("app.services.knowledge.graph_service.graph_service.top_hubs", return_value=[]):
        resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
        assert resp.status_code == 200
        data = resp.json()
        assert data["top_n"] == 1
        assert data["hubs"] == []


def test_get_top_hub_with_date_and
