# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Scope (read-only, no infra):**  
Add `GET /api/v1/cost-anomaly/signal/top-hub` (optional `?for_date=YYYY-MM-DD`) that returns today’s strongest hub insight as a cost-anomaly signal (Sense + Signal; no Execute). Uses real knowledge graph when available; deterministic fallback when not. No writes, no schema migrations, deployable via existing Docker compose.

---

### 1) File layout (canonical)

```
/opt/axentx/Costinel/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── endpoints/
│   │   │   │   ├── cost_anomaly.py
│   │   │   │   └── __init__.py
│   │   │   └── __init__.py
│   │   └── __init__.py
│   ├── services/
│   │   ├── knowledge_graph.py
│   │   └── __init__.py
│   ├── models/
│   │   └── signal.py
│   └── core/
│       └── config.py
├── tests/
│   └── api/
│       └── v1/
│           └── test_cost_anomaly.py
├── docker-compose.yml
└── requirements.txt
```

---

### 2) Concrete changes (merged + hardened)

#### A) Model — `app/models/signal.py`
```python
from datetime import date
from typing import List
from pydantic import BaseModel, Field


class RelatedDoc(BaseModel):
    slug: str
    title: str
    score: float = Field(ge=0.0, le=1.0)


class TopHubSignal(BaseModel):
    hub: str
    hub_slug: str
    date: date
    strength: float = Field(ge=0.0, le=1.0, description="Connection strength (0-1)")
    related_docs: List[RelatedDoc] = Field(default_factory=list, max_items=10)
    source: str = Field(default="knowledge-graph", description="Source of signal")
    message: str | None = None
```

---

#### B) Service — `app/services/knowledge_graph.py`
```python
import logging
from datetime import date
from typing import Optional
from app.models.signal import RelatedDoc, TopHubSignal

logger = logging.getLogger(__name__)

_FALLBACK_HUBS = [
    {
        "hub": "MOC",
        "hub_slug": "moc",
        "strength": 0.92,
        "related_docs": [
            {"slug": "moc/change-control", "title": "MOC Change Control Process", "score": 0.88},
            {"slug": "moc/cost-review", "title": "MOC Cost Review Checklist", "score": 0.81},
        ],
    },
    {
        "hub": "Cloud Governance",
        "hub_slug": "cloud-governance",
        "strength": 0.85,
        "related_docs": [
            {"slug": "cloud-governance/ri-optimization", "title": "RI Optimization Guide", "score": 0.79},
        ],
    },
]


class KnowledgeGraphClient:
    """
    Lightweight client to fetch today's top hub.
    Replace `_query_graph` with real graph integration (Neo4j/FalkorDB/RAG) when available.
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def get_top_hub(self, for_date: Optional[date] = None) -> TopHubSignal:
        target_date = for_date or date.today()

        if self.enabled:
            try:
                return self._query_graph(target_date)
            except Exception as exc:
                logger.warning("Graph query failed, using fallback: %s", exc)

        return self._fallback(target_date)

    def _query_graph(self, for_date: date) -> TopHubSignal:
        # TODO: integrate real graph.
        # Example (Neo4j):
        #   result = self.driver.execute_query(
        #       """
        #       MATCH (h:Hub)-[r:RELATED_TO]->(d:Doc)
        #       WHERE date($for_date) = date($for_date)  // scope if needed
        #       RETURN h.name AS hub, h.slug AS hub_slug,
        #              sum(r.weight) AS strength,
        #              collect({slug: d.slug, title: d.title, score: r.weight})[..10] AS related_docs
        #       ORDER BY strength DESC
        #       LIMIT 1
        #       """,
        #       for_date=for_date.isoformat()
        #   )
        #   ...map to TopHubSignal...
        raise NotImplementedError("Real graph integration not yet implemented")

    def _fallback(self, for_date: date) -> TopHubSignal:
        best = max(_FALLBACK_HUBS, key=lambda x: x["strength"])
        return TopHubSignal(
            hub=best["hub"],
            hub_slug=best["hub_slug"],
            date=for_date,
            strength=best["strength"],
            related_docs=[RelatedDoc(**doc) for doc in best["related_docs"]],
            source="fallback",
            message="Using deterministic fallback; enable graph for live top-hub.",
        )
```

---

#### C) Endpoint — `app/api/v1/endpoints/cost_anomaly.py`
```python
from datetime import date
from fastapi import APIRouter, Depends, Query
from app.services.knowledge_graph import KnowledgeGraphClient
from app.models.signal import TopHubSignal
from app.core.config import settings

router = APIRouter()


def get_kg_client() -> KnowledgeGraphClient:
    return KnowledgeGraphClient(enabled=settings.KNOWLEDGE_GRAPH_ENABLED)


@router.get(
    "/cost-anomaly/signal/top-hub",
    response_model=TopHubSignal,
    summary="Top hub signal for cost anomaly context",
    description=(
        "Returns the most-connected hub for a date (default today) as a cost-anomaly signal. "
        "Sense + Signal only — no execution. Uses knowledge graph when enabled; "
        "deterministic fallback otherwise."
    ),
)
def get_top_hub_signal(
    for_date: date | None = Query(default=None, description="Date (defaults to today)"),
    kg: KnowledgeGraphClient = Depends(get_kg_client),
) -> TopHubSignal:
    return kg.get_top_hub(for_date=for_date)
```

Wire into `app/api/v1/endpoints/__init__.py`:
```python
from . import cost_anomaly
```

Wire into `app/api/v1/__init__.py`:
```python
from fastapi import APIRouter
from app.api.v1.endpoints import cost_anomaly

api_router = APIRouter()
api_router.include_router(cost_anomaly.router, prefix="", tags=["cost-anomaly"])
```

Wire into `app/main.py` (if not already):
```python
from fastapi import FastAPI
from app.api.v1 import api_router
from app.core.config import settings

app = FastAPI(title=settings.PROJECT_NAME)
app.include_router(api_router, prefix="/api/v1")
```

---

#### D) Config — `app/core/config.py` (add flag)
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "Costinel"
    KNOWLEDGE_GRAPH_ENABLED: bool = False  # enable when real graph is available

    class Config:
        env_file = ".env"

settings = Settings()
```

---

#### E) Tests — `tests/api/v1/test_cost_anomaly.py`
```python
from fastapi.testclient import TestClient
from app.main import app
from datetime import date

client = TestClient(app)


def test_get_top_hub_signal_fallback():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()

    assert "hub" in data
    assert "hub_slug" in data
    assert data["date"] ==
