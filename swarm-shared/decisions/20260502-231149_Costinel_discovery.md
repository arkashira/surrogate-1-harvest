# Costinel / discovery

## Implementation Plan — Costinel `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope**  
- Read-only endpoint (Sense + Signal). No mutations, no side effects.  
- Optional `?for_date=YYYY-MM-DD` (default today).  
- Reuse existing knowledge-graph assets; avoid recomputing heavy graph metrics per request.  
- Return compact JSON suitable for dashboard widgets and alerting.

**Estimated effort:** ~90 min (code + tests + smoke).

---

### 1) Design

**Endpoint**  
`GET /api/v1/cost-anomaly/signal/top-hub`

**Query params**  
- `for_date` (optional, `YYYY-MM-DD`, default: today) — date to anchor signal.

**Response (200)**  
```json
{
  "signal_type": "top_hub",
  "generated_at": "2026-05-03T14:23:00Z",
  "for_date": "2026-05-03",
  "hub": {
    "id": "MOC",
    "label": "MOC",
    "type": "cost_center",
    "score": 0.92,
    "rank": 1,
    "degree": 47,
    "weighted_degree": 128.4,
    "context": {
      "summary": "MOC is the most-connected hub for 2026-05-03 with elevated cross-account spend links.",
      "related_signals": [
        { "type": "cost_spike", "severity": "medium", "resource": "prod-analytics-east-1" },
        { "type": "ri_underutilized", "severity": "low", "resource": "moc-shared-ri-pool" }
      ]
    }
  },
  "audit": {
    "graph_version": "v2026-05-03-01",
    "source": "knowledge_rag_graph"
  }
}
```

**Errors**  
- `400` — invalid `for_date` format.  
- `404` — no graph snapshot for requested date.  
- `500` — internal error (with trace id).

---

### 2) Implementation Steps

1. Add FastAPI route in `app/api/v1/cost_anomaly.py` (or create if missing).  
2. Add dependency to load graph snapshot for `for_date` (cached, read-only).  
3. Compute top hub by weighted degree (fast, deterministic).  
4. Enrich with lightweight context from existing RAG/doc store (avoid heavy LLM calls).  
5. Return standardized payload.  
6. Add minimal unit test and a smoke curl script.

---

### 3) Code Snippets

#### `app/api/v1/cost_anomaly.py`
```python
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.graph import load_graph_snapshot, top_hub_by_weighted_degree
from app.services.rag import hub_context_for_date

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])


class RelatedSignal(BaseModel):
    type: str
    severity: str
    resource: Optional[str] = None


class HubContext(BaseModel):
    summary: str
    related_signals: list[RelatedSignal] = Field(default_factory=list)


class HubSignal(BaseModel):
    id: str
    label: str
    type: str
    score: float
    rank: int
    degree: int
    weighted_degree: float
    context: HubContext


class TopHubSignalResponse(BaseModel):
    signal_type: str = "top_hub"
    generated_at: str
    for_date: str
    hub: HubSignal
    audit: dict


def _parse_date(d: Optional[str]) -> date:
    if d is None:
        return date.today()
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid for_date, expected YYYY-MM-DD") from exc


@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
async def get_top_hub_signal(
    for_date: Optional[str] = Query(None, description="Anchor date (YYYY-MM-DD), defaults to today"),
):
    target_date = _parse_date(for_date)
    snapshot_path = f"knowledge_rag/graph_snapshots/{target_date.isoformat()}.json"

    G = load_graph_snapshot(snapshot_path)
    if G is None:
        raise HTTPException(status_code=404, detail=f"No graph snapshot for {target_date.isoformat()}")

    node_id, metrics = top_hub_by_weighted_degree(G)
    context = hub_context_for_date(node_id, target_date)

    payload = TopHubSignalResponse(
        generated_at=datetime.utcnow().isoformat() + "Z",
        for_date=target_date.isoformat(),
        hub=HubSignal(
            id=node_id,
            label=metrics.get("label", node_id),
            type=metrics.get("type", "unknown"),
            score=float(metrics.get("score", 0.0)),
            rank=1,
            degree=int(metrics.get("degree", 0)),
            weighted_degree=float(metrics.get("weighted_degree", 0.0)),
            context=context,
        ),
        audit={
            "graph_version": metrics.get("graph_version", "unknown"),
            "source": "knowledge_rag_graph",
        },
    )
    return payload
```

#### `app/services/graph.py` (minimal additions)
```python
import json
from pathlib import Path
from typing import Dict, Tuple, Optional
import networkx as nx

SNAPSHOT_ROOT = Path(__file__).parent.parent.parent / "knowledge_rag" / "graph_snapshots"


def load_graph_snapshot(snapshot_path: str) -> Optional[nx.Graph]:
    p = SNAPSHOT_ROOT / Path(snapshot_path).name
    if not p.is_file():
        return None
    with open(p, "r") as f:
        data = json.load(f)
    return nx.node_link_graph(data)


def top_hub_by_weighted_degree(G: nx.Graph) -> Tuple[str, Dict]:
    # Prefer weighted_degree if present; fallback to degree
    best_node = None
    best_score = -1
    best_metrics = {}

    for n in G.nodes(data=True):
        node_id = n[0]
        attrs = n[1]
        wdeg = float(attrs.get("weighted_degree", attrs.get("degree", 0)))
        deg = int(attrs.get("degree", 0))
        if wdeg > best_score:
            best_score = wdeg
            best_node = node_id
            best_metrics = {
                "label": str(attrs.get("label", node_id)),
                "type": str(attrs.get("type", "unknown")),
                "score": float(attrs.get("score", 0.0)),
                "degree": deg,
                "weighted_degree": wdeg,
                "graph_version": attrs.get("graph_version", "unknown"),
            }

    if best_node is None:
        # fallback: pick node with max degree
        best_node = max(G.degree, key=lambda x: x[1])[0]
        best_metrics = {"degree": G.degree[best_node], "weighted_degree": 0.0, "label": best_node, "type": "unknown"}

    return best_node, best_metrics
```

#### `app/services/rag.py` (lightweight enrichment)
```python
from datetime import date
from typing import Dict, Any

from app.services.rag_store import query_related_signals  # assume exists or stub


def hub_context_for_date(hub_id: str, target_date: date) -> Dict[str, Any]:
    # Lightweight: query existing RAG store for recent items linked to hub
    related = query_related_signals(hub_id, target_date, limit=5)

    summary = (
        f"{hub_id} is the most-connected hub for {target_date.isoformat()} "
        f"with {len(related)} related signals."
    )

    return {
        "summary": summary,
        "related_signals":
