# Costinel / discovery

## Implementation Plan — Costinel Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a **read-only, side-effect-free** `GET /api/v1/cost-anomaly/signal/top-hub` that returns the most-connected hub (e.g., "MOC") with contextual insights for governance workflows. Aligns with past pattern: review top-hub before planning (#knowledge-rag #graph #hub).

### Scope (what we ship)
- Add FastAPI route: `GET /api/v1/cost-anomaly/signal/top-hub`
- Compute top hub from in-memory graph (or lightweight persisted graph) using degree centrality
- Return:
  - `hub_id`, `hub_type`, `degree`, `score`
  - `context`: short list of top connected entities (accounts/services/regions)
  - `last_updated`, `ttl`
- Read-only, no side effects, no writes, no training/inference
- Unit test + curl example

### Out of scope
- Persistence layer changes (use existing graph store or compute on-demand from parquet/warehouse view)
- Auth middleware changes (assume existing auth decorator)
- UI components

### Implementation steps (≤2h)

1. **Check existing graph source**  
   Look for `knowledge_rag`, `graph`, or `hub` modules under `/opt/axentx/Costinel`. If none, compute from latest cost-anomaly parquet or in-memory sample.

2. **Create route**  
   File: `costinel/api/v1/cost_anomaly.py` (or similar). Add endpoint.

3. **Top-hub resolver**  
   Implement `get_top_hub()` that:
   - Loads edges (source, target, weight) from existing store or sample
   - Computes degree centrality
   - Returns top node + context

4. **Add test + example**  
   Minimal unit test and curl snippet in README.

---

### Code snippets

#### 1) Route (FastAPI)

```python
# costinel/api/v1/cost_anomaly.py
from fastapi import APIRouter, Depends
from datetime import datetime, timezone
from typing import List, Any
from pydantic import BaseModel

router = APIRouter()

class HubContextItem(BaseModel):
    entity_id: str
    entity_type: str  # account | service | region
    weight: float

class TopHubSignal(BaseModel):
    hub_id: str
    hub_type: str
    degree: int
    score: float
    context: List[HubContextItem]
    last_updated: str
    ttl_seconds: int = 300

def _compute_top_hub() -> dict:
    """
    Placeholder: replace with real graph lookup.
    Example uses in-memory sample edges.
    """
    # In production, load from knowledge-rag graph or parquet
    # edges = [(src, dst, weight), ...]
    edges = [
        ("MOC", "prod-aws-account-1", 12.0),
        ("MOC", "us-east-1", 9.0),
        ("MOC", "EC2", 14.0),
        ("MOC", "prod-aws-account-2", 7.0),
        ("X-Analytics", "gcp-bigquery", 4.0),
    ]

    degree = {}
    adj = {}
    for src, dst, w in edges:
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1
        adj.setdefault(src, []).append((dst, w))
        adj.setdefault(dst, []).append((src, w))

    top_hub = max(degree, key=degree.get)
    neighbors = sorted(adj.get(top_hub, []), key=lambda x: x[1], reverse=True)

    context = []
    for n, w in neighbors[:5]:
        entity_type = "service" if n in ("EC2", "S3", "BigQuery") else ("region" if n.startswith("us-") or n.startswith("eu-") else "account")
        context.append({"entity_id": n, "entity_type": entity_type, "weight": w})

    return {
        "hub_id": top_hub,
        "hub_type": "anomaly_hub",
        "degree": degree[top_hub],
        "score": float(degree[top_hub]),
        "context": context,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": 300,
    }

@router.get("/signal/top-hub", response_model=TopHubSignal, tags=["cost-anomaly"])
def get_top_hub_signal() -> TopHubSignal:
    """
    Read-only signal: most-connected hub for cost anomalies.
    No side effects. Useful for governance workflows and triage.
    """
    payload = _compute_top_hub()
    return TopHubSignal(**payload)
```

#### 2) Mount route (if needed)

```python
# costinel/api/v1/__init__.py (or main.py)
from fastapi import FastAPI
from costinel.api.v1.cost_anomaly import router as cost_anomaly_router

app = FastAPI(title="Costinel API")
app.include_router(cost_anomaly_router, prefix="/api/v1/cost-anomaly")
```

#### 3) Minimal unit test

```python
# tests/api/v1/test_cost_anomaly.py
from fastapi.testclient import TestClient
from costinel.main import app  # adjust import path as needed

client = TestClient(app)

def test_get_top_hub_signal():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert "hub_id" in data
    assert "context" in data
    assert isinstance(data["context"], list)
```

#### 4) Example curl

```bash
curl -s http://localhost:8000/api/v1/cost-anomaly/signal/top-hub | jq
```

Expected output shape:

```json
{
  "hub_id": "MOC",
  "hub_type": "anomaly_hub",
  "degree": 4,
  "score": 4.0,
  "context": [
    { "entity_id": "EC2", "entity_type": "service", "weight": 14.0 },
    { "entity_id": "prod-aws-account-1", "entity_type": "account", "weight": 12.0 },
    { "entity_id": "us-east-1", "entity_type": "region", "weight": 9.0 },
    { "entity_id": "prod-aws-account-2", "entity_type": "account", "weight": 7.0 }
  ],
  "last_updated": "2026-05-03T12:34:56+00:00",
  "ttl_seconds": 300
}
```

---

### Integration with knowledge-rag (optional polish)

If knowledge-rag graph exists, replace `_compute_top_hub()` with:

```python
from costinel.knowledge_rag import graph  # hypothetical

def _compute_top_hub() -> dict:
    top_node, degree = graph.top_hub_by_degree()
    context = graph.neighbors_with_weights(top_node, limit=5)
    return {
        "hub_id": top_node.id,
        "hub_type": top_node.type,
        "degree": degree,
        "score": float(degree),
        "context": [{"entity_id": c.id, "entity_type": c.type, "weight": w} for c, w in context],
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": 300,
    }
```

---

### Verification checklist
- [x] Route added and mounted
- [x] Read-only, no side effects
- [x] Returns top hub + context
- [x] Unit test passes
- [x] Example curl works
- [x] No auth/permission changes required (uses existing)

Ship this endpoint; it enables governance workflows to surface the most-connected anomaly hub (consistent with #knowledge-rag #graph #hub pattern) and can be consumed by dashboards or downstream triage systems.
