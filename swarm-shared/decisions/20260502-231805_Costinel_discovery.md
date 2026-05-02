# Costinel / discovery

## Final Implementation Plan — `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & constraints**
- Read-only, no side effects.
- Optional `?for_date=YYYY-MM-DD` (default today UTC) and `?top_n` (1–10, default 1).
- Reuse existing knowledge-graph assets (top-hub pattern) to return the most-connected hub(s) with contextual insights for cost-anomaly signals.
- Fast path: pre-computed hub cache (JSON) + lightweight enrichment; fallback to on-demand graph query if cache miss.
- No DB writes; observability via structured logs only.

**Estimated effort**: 90–110 minutes (implementation + tests + smoke).

---

### 1) Implementation steps

1. Add route and handler (FastAPI assumed; adapt if Flask).
2. Implement `TopHubService` that:
   - Normalizes `for_date` to UTC day.
   - Reads pre-computed hub cache from `knowledge_rag/hubs/{date}/top_hubs.json` (or similar).
   - If missing, runs lightweight graph query against existing graph store (Neo4j / NetworkX file) to compute top N by degree/weight.
   - Enriches each hub with summary, top 3 connected docs, and last signal timestamp.
3. Add schema models (`TopHubResponse`, `HubNode`, `HubEdgeSummary`).
4. Add unit tests for query params, cache hit/miss, and validation.
5. Add structured logging and metrics counters (`costinel.top_hub_requests`, `costinel.top_hub_cache_hit`).
6. Smoke test via `curl` and verify idempotency.

---

### 2) Code snippets

#### `app/api/v1/endpoints/cost_anomaly.py`
```python
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, conint

from app.services.top_hub_service import TopHubService

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


class HubNode(BaseModel):
    hub_id: str = Field(..., description="Hub identifier (e.g., MOC)")
    label: str = Field(..., description="Human-readable label")
    degree: int = Field(..., description="Graph degree or connection weight")
    summary: str = Field(..., description="Short contextual summary")
    top_connected_docs: List[str] = Field(..., description="Top connected doc slugs or titles")
    last_signal_at: datetime | None = Field(None, description="Last signal timestamp for this hub")


class TopHubResponse(BaseModel):
    for_date: str = Field(..., description="UTC date (YYYY-MM-DD)")
    top_n: int = Field(..., description="Requested top N")
    hubs: List[HubNode] = Field(..., description="Top hub nodes")
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@router.get("/signal/top-hub", response_model=TopHubResponse)
async def get_top_hub(
    for_date: str | None = Query(
        None,
        description="UTC date (YYYY-MM-DD). Defaults to today.",
        regex=r"^\d{4}-\d{2}-\d{2}$",
    ),
    top_n: conint(ge=1, le=10) = Query(1, description="Number of top hubs to return (1-10)."),
):
    # Normalize date
    try:
        if for_date:
            target = datetime.strptime(for_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            target = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid for_date: {exc}") from exc

    try:
        hubs = TopHubService.get_top_hubs(for_date=target.date(), top_n=top_n)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Top-hub data not available for the requested date.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to compute top hubs: {exc}") from exc

    return TopHubResponse(
        for_date=target.date().isoformat(),
        top_n=top_n,
        hubs=hubs,
    )
```

#### `app/services/top_hub_service.py`
```python
import json
import logging
from datetime import date
from pathlib import Path
from typing import List

from app.models.top_hub import HubNode

logger = logging.getLogger(__name__)

# Expected cache location; adjust to your repo layout.
CACHE_ROOT = Path(__file__).parent.parent.parent.parent / "knowledge_rag" / "hubs"


class TopHubService:
    @staticmethod
    def get_top_hubs(for_date: date, top_n: int) -> List[HubNode]:
        cache_path = CACHE_ROOT / for_date.isoformat() / f"top_hubs.json"

        if cache_path.is_file():
            logger.info("costinel.top_hub_cache_hit", extra={"date": for_date.isoformat(), "cache": True})
            with cache_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            logger.info("costinel.top_hub_cache_hit", extra={"date": for_date.isoformat(), "cache": False})
            raw = TopHubService._compute_top_hubs(for_date=for_date, top_n=top_n)

        # raw expected: [{"hub_id": "...", "label": "...", "degree": 123, "summary": "...", "top_connected_docs": [...], "last_signal_at": "..."}, ...]
        hubs = [
            HubNode(
                hub_id=item["hub_id"],
                label=item["label"],
                degree=item["degree"],
                summary=item["summary"],
                top_connected_docs=item.get("top_connected_docs", []),
                last_signal_at=item.get("last_signal_at"),
            )
            for item in raw[:top_n]
        ]
        return hubs

    @staticmethod
    def _compute_top_hubs(for_date: date, top_n: int) -> List[dict]:
        """
        Lightweight fallback: load existing graph asset and compute top N by degree/weight.
        Replace with your actual graph loading logic (Neo4j, NetworkX, etc.).
        """
        graph_path = Path(__file__).parent.parent.parent.parent / "knowledge_rag" / "graph" / f"{for_date.isoformat()}_graph.json"
        if not graph_path.is_file():
            raise FileNotFoundError(f"No graph or cache available for {for_date.isoformat()}")

        with graph_path.open("r", encoding="utf-8") as f:
            graph = json.load(f)

        # Simple degree computation on nodes with edges list.
        node_scores = {}
        for edge in graph.get("edges", []):
            a, b = edge.get("source"), edge.get("target")
            w = edge.get("weight", 1)
            node_scores[a] = node_scores.get(a, 0) + w
            node_scores[b] = node_scores.get(b, 0) + w

        top_keys = sorted(node_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

        # Map to node metadata
        node_by_id = {n["id"]: n for n in graph.get("nodes", [])}
        results = []
        for hub_id, degree in top_keys:
            meta = node_by_id.get(hub_id, {})
            results.append(
                {
                    "hub_id": hub_id,
                    "label": meta.get("label", hub_id),
                    "degree": degree,
                    "summary": meta.get("summary", f"Top hub {hub_id} for cost signals."),
                    "top_connected_docs": meta.get("top_connected_docs", []),
                    "last_signal_at": meta.get("last_signal_at"),
                }
            )
        return results
```

#### `app/models/top_hub.py`
```python
from pydantic import BaseModel
from typing import List, Optional


class HubNode(BaseModel):
    hub_id: str
    label: str
    degree: int
    summary: str
    top_connected_docs: List[str]
    last_signal_at: Optional[str] = None
```

---

### 3) Tests (pytest)

#### `tests/api/test
