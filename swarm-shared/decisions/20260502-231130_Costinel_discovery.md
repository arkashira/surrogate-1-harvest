# Costinel / discovery

## Final Implementation Plan — `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & constraints**
- Read-only, no side effects.
- Optional `?for_date=YYYY-MM-DD` (default today) and `?top_n` (1–10, default 1).
- Reuse existing knowledge-graph assets; do not recompute heavy analytics or mutate graphs.
- Fast: target <200 ms p95 via in-memory graph cache and lightweight projection.
- Return top hub(s) with centrality, related docs, and a short actionable insight.
- Non-goals: mutations, auth changes, graph rebuilds, write-side effects.

---

### 1) Endpoint contract

**Path**  
`GET /api/v1/cost-anomaly/signal/top-hub`

**Query parameters**
- `for_date` (optional, regex `^\d{4}-\d{2}-\d{2}$`) — date in YYYY-MM-DD; defaults to today.
- `top_n` (optional, int, 1–10) — number of top hubs to return; defaults to 1.

**Responses**
- `200 OK` — list of top hubs with centrality, insight, and related docs.
- `400 Bad Request` — invalid `for_date` or `top_n`.
- `404 Not Found` — no knowledge graph available for the requested date (and no fallback).
- `500 Internal Server Error` — unexpected failure.

---

### 2) Concrete implementation (≤2 h)

1. **Add route + handler** (`app/api/v1/endpoints/cost_anomaly.py`)  
   - FastAPI router with typed query params and Pydantic response models.
   - Validate inputs; return 400 on invalid formats.
   - Default `for_date` to `date.today()` when omitted.

2. **Add thin service layer** (`app/services/cost_anomaly/top_hub.py`)  
   - `top_hub_signal(target_date: date, top_n: int) -> List[TopHubSignal]`
   - Load graph: try date-specific graph first; if unavailable, use latest cached graph.
   - If no graph at all, return empty list (caller maps to 404).
   - Compute top hubs by **weighted degree centrality** (prefer weighted; fallback to degree).
   - For each hub:
     - Collect immediate neighbors as related docs.
     - Compute contribution proxy: `edge_weight * neighbor_degree`.
     - Sort related docs by contribution descending.
     - Produce concise insight: hub title, centrality, date, and top related item (if any).
   - Keep all calculations lightweight; avoid recursive or global recomputations.

3. **Add minimal unit tests** (`tests/api/test_cost_anomaly_top_hub.py`)  
   - Happy path: 200 with expected schema.
   - With explicit date param.
   - Invalid date → 400.
   - Missing graph for date → 404 (or 200 empty list if fallback behavior desired; prefer 404 for clarity).

4. **Update docs & verify**  
   - FastAPI auto-generates OpenAPI at `/docs`; verify schema and examples.
   - Smoke-test locally with real graph; confirm p95 latency <200 ms.

---

### 3) Code (merged strongest parts)

#### `app/api/v1/endpoints/cost_anomaly.py`
```python
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.cost_anomaly.top_hub import RelatedDoc, TopHubSignal, top_hub_signal

router = APIRouter()


class RelatedDocResponse(BaseModel):
    doc_id: str
    title: str
    edge_label: str
    centrality_contrib: float


class TopHubSignalResponse(BaseModel):
    for_date: str
    hub_id: str
    hub_title: str
    centrality: float
    insight: str
    related_docs: List[RelatedDocResponse]


@router.get(
    "/cost-anomaly/signal/top-hub",
    response_model=List[TopHubSignalResponse],
    tags=["Cost Anomaly"],
)
async def get_top_hub_signal(
    for_date: Optional[str] = Query(
        None,
        regex=r"^\d{4}-\d{2}-\d{2}$",
        description="Date in YYYY-MM-DD format (defaults to today)",
    ),
    top_n: int = Query(1, ge=1, le=10, description="Number of top hubs to return"),
) -> List[TopHubSignalResponse]:
    try:
        target_date = date.fromisoformat(for_date) if for_date else date.today()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid for_date format. Use YYYY-MM-DD.") from exc

    signals = top_hub_signal(target_date=target_date, top_n=top_n)
    if not signals:
        raise HTTPException(
            status_code=404,
            detail=f"No knowledge graph available for date {target_date.isoformat()}",
        )

    return [
        TopHubSignalResponse(
            for_date=target_date.isoformat(),
            hub_id=s.hub_id,
            hub_title=s.hub_title,
            centrality=s.centrality,
            insight=s.insight,
            related_docs=[
                RelatedDocResponse(
                    doc_id=r.doc_id,
                    title=r.title,
                    edge_label=r.edge_label,
                    centrality_contrib=r.centrality_contrib,
                )
                for r in s.related_docs
            ],
        )
        for s in signals
    ]
```

#### `app/services/cost_anomaly/top_hub.py`
```python
from datetime import date
from typing import Dict, List, Optional, Tuple

from app.graph.knowledge import KnowledgeGraph  # assumed existing interface
from app.graph.analytics import degree_centrality, weighted_degree_centrality  # assumed helpers


class RelatedDoc:
    def __init__(self, doc_id: str, title: str, edge_label: str, centrality_contrib: float):
        self.doc_id = doc_id
        self.title = title
        self.edge_label = edge_label
        self.centrality_contrib = centrality_contrib


class TopHubSignal:
    def __init__(
        self,
        hub_id: str,
        hub_title: str,
        centrality: float,
        insight: str,
        related_docs: List[RelatedDoc],
    ):
        self.hub_id = hub_id
        self.hub_title = hub_title
        self.centrality = centrality
        self.insight = insight
        self.related_docs = related_docs


def top_hub_signal(target_date: date, top_n: int = 1) -> List[TopHubSignal]:
    """
    Return top hubs by weighted degree centrality for the requested date.
    Falls back to latest cached graph if date-specific graph is unavailable.
    """
    graph = KnowledgeGraph.for_date(target_date) or KnowledgeGraph.latest()
    if graph is None:
        return []

    # Prefer weighted centrality; fallback to degree centrality.
    centrality = weighted_degree_centrality(graph) or degree_centrality(graph)
    if not centrality:
        return []

    hubs: List[Tuple[str, float]] = sorted(centrality.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    results: List[TopHubSignal] = []
    for hub_id, score in hubs:
        hub_node = graph.get_node(hub_id)
        hub_title = hub_node.get("title", hub_id) if hub_node else hub_id

        related: List[RelatedDoc] = []
        neighbors = graph.neighbors(hub_id)
        for nb_id, edge in neighbors:
            nb_node = graph.get_node(nb_id)
            if nb_node is None:
                continue
            edge_weight = float(edge.get("weight", 1.0))
            nb_deg = centrality.get(nb_id, 1.0)
            contrib = edge_weight * nb_deg
            related.append(
                RelatedDoc(
                    doc_id=nb_id,
                    title=nb_node.get("title", nb_id),
                    edge_label=edge.get("label", "related"),
                    centrality_contrib=round(contrib, 4),
                )
            )
        related.sort(key=lambda r: r.centrality_contrib, reverse=True)

        top_related
