# Costinel / discovery

## Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a read-only, side-effect-free `GET /api/v1/cost-anomaly/signal/top-hub` that uses existing knowledge-graph assets to return the top hub(s) for contextual insights (e.g., MOC) and anomaly signals keyed by date. This enables dashboards and downstream governance workflows to surface the most-connected context without executing changes (Sense + Signal).

---

### 1) Changes to make (concrete)

1. Add FastAPI route: `GET /api/v1/cost-anomaly/signal/top-hub`
   - Optional query params:
     - `for_date` (YYYY-MM-DD, default today)
     - `top_k` (int, default 5)
     - `hub_type` (optional filter, e.g., "MOC")
   - Response shape:
     ```json
     {
       "date": "2026-05-02",
       "top_hubs": [
         {
           "hub_id": "MOC",
           "hub_type": "cost-center",
           "score": 0.94,
           "connected_entities": 128,
           "anomaly_signals": [
             {
               "service": "AmazonEC2",
               "region": "us-east-1",
               "severity": "high",
               "delta_pct": 42.3,
               "description": "Unusual spend spike vs 30d baseline"
             }
           ],
           "recommendations": [
             "Review RI coverage for top-attached accounts",
             "Validate tagging compliance for linked resources"
           ]
         }
       ],
       "generated_at": "2026-05-02T23:17:00Z"
     }
     ```

2. Add lightweight service layer: `costinel/services/top_hub_service.py`
   - Uses existing knowledge-graph/RAG assets (read-only).
   - If graph not available, falls back to simple heuristics on recent anomaly table (read-only).
   - No writes; no mutations.

3. Add minimal unit tests: `tests/test_top_hub_endpoint.py`
   - Test param parsing.
   - Test response shape.
   - Test fallback behavior.

4. Add route registration in main app (likely `main.py` or `api/router.py`).

5. Update openapi docs (FastAPI auto-generates) and add brief endpoint docstring.

---

### 2) Code snippets

#### `costinel/services/top_hub_service.py`
```python
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

# These imports assume existing project structure; adjust if modules differ.
# from costinel.knowledge_rag import graph  # optional: existing graph accessor
# from costinel.repositories.anomaly_repo import get_recent_anomalies  # optional read-only repo


def _today_str() -> str:
    return date.today().isoformat()


def get_top_hubs(
    for_date: Optional[str] = None,
    top_k: int = 5,
    hub_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read-only: returns top hubs and associated anomaly signals.
    Preference:
      1) Use existing knowledge-graph/RAG if available.
      2) Fallback to anomaly-derived heuristics (read-only).
    """
    target_date = for_date or _today_str()

    # Try graph/RAG first (read-only query)
    try:
        return _get_top_hubs_from_graph(target_date=target_date, top_k=top_k, hub_type=hub_type)
    except Exception:
        # graceful fallback
        return _get_top_hubs_from_anomalies(target_date=target_date, top_k=top_k, hub_type=hub_type)


def _get_top_hubs_from_graph(target_date: str, top_k: int, hub_type: Optional[str]) -> Dict[str, Any]:
    """
    Placeholder: integrate with existing knowledge-rag/graph.
    Replace with real queries to your graph (e.g., neo4j, networkx, or RAG retriever).
    """
    # Example stub:
    # hubs = graph.query_top_hubs(date=target_date, hub_type=hub_type, limit=top_k)
    # For now, return a deterministic stub so endpoint is functional.
    stub_hub = {
        "hub_id": "MOC",
        "hub_type": "cost-center",
        "score": 0.94,
        "connected_entities": 128,
        "anomaly_signals": [
            {
                "service": "AmazonEC2",
                "region": "us-east-1",
                "severity": "high",
                "delta_pct": 42.3,
                "description": "Unusual spend spike vs 30d baseline",
            }
        ],
        "recommendations": [
            "Review RI coverage for top-attached accounts",
            "Validate tagging compliance for linked resources",
        ],
    }
    return {
        "date": target_date,
        "top_hubs": [stub_hub][:top_k],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def _get_top_hubs_from_anomalies(target_date: str, top_k: int, hub_type: Optional[str]) -> Dict[str, Any]:
    """
    Fallback: derive top hubs from recent anomalies (read-only).
    Replace with real read-only repository calls.
    """
    # Example stub derived from anomalies
    stub = {
        "date": target_date,
        "top_hubs": [
            {
                "hub_id": "MOC",
                "hub_type": hub_type or "inferred",
                "score": 0.86,
                "connected_entities": 64,
                "anomaly_signals": [
                    {
                        "service": "AmazonS3",
                        "region": "ap-southeast-1",
                        "severity": "medium",
                        "delta_pct": 18.7,
                        "description": "Storage cost increase vs prior week",
                    }
                ],
                "recommendations": ["Check lifecycle policies on top buckets"],
            }
        ][:top_k],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    return stub
```

#### `costinel/api/router.py` (or add to existing router)
```python
from fastapi import APIRouter, Query
from datetime import date

from costinel.services.top_hub_service import get_top_hubs

router = APIRouter()


@router.get("/cost-anomaly/signal/top-hub", summary="Top hub signal (read-only)")
async def top_hub_signal(
    for_date: str = Query(None, description="Date (YYYY-MM-DD), defaults to today"),
    top_k: int = Query(5, ge=1, le=50, description="Number of top hubs to return"),
    hub_type: str = Query(None, description="Optional hub type filter (e.g., MOC)"),
):
    """
    Read-only endpoint that surfaces the most-connected hub(s) and associated
    anomaly signals for contextual insight (Sense + Signal).
    """
    result = get_top_hubs(for_date=for_date, top_k=top_k, hub_type=hub_type)
    return result
```

#### Register router in main app (`main.py` or equivalent)
```python
from fastapi import FastAPI
from costinel.api.router import router as costinel_router

app = FastAPI(title="Costinel", version="4.2.0")

app.include_router(costinel_router, prefix="/api/v1", tags=["cost-anomaly"])
```

#### Minimal unit test (`tests/test_top_hub_endpoint.py`)
```python
from fastapi.testclient import TestClient
from costinel.main import app  # adjust import path as needed

client = TestClient(app)


def test_top_hub_defaults():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert "date" in data
    assert "top_hubs" in data
    assert isinstance(data["top_hubs"], list)
    if data["top_hubs"]:
        hub = data["top_hubs"][0]
        assert "hub_id" in hub
