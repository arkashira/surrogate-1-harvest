# Costinel / discovery

## Implementation Plan — Costinel Quality Increment (<2h)

**Scope (read-only, no infra):**  
Add `GET /api/v1/cost-anomaly/signal/top-hub` (optional `?for_date=YYYY-MM-DD`) that returns today’s strongest hub insight from the knowledge graph as a cost-anomaly signal (Sense + Signal; no execution). Uses existing knowledge-rag/graph pipeline and MOC-style top-hub review pattern.

**Why this is highest-value (<2h):**
- Pure read-only endpoint — no infra, no migrations, no secrets.
- Reuses existing knowledge-rag/graph tooling (pattern: top-hub doc insight).
- Immediate user value: surfaces strongest contextual insight on the cost dashboard / anomaly feed.
- Fits Costinel philosophy: Sense + Signal (no Execute).

---

### Steps (est. 60–90 min)

1. Add FastAPI route `GET /api/v1/cost-anomaly/signal/top-hub`
   - Accept optional `for_date` (default: today UTC).
   - Validate date format, clamp to reasonable range.
   - Return 200 with signal payload; 404 if no hub found; 400 on bad params.

2. Implement thin service `CostAnomalySignalService.top_hub_signal(for_date)`
   - Call existing knowledge-rag component to fetch top hub (e.g., `knowledge_rag.top_hub(for_date)`).
   - Normalize to canonical signal shape:
     - `hub_id`, `hub_label`, `hub_type`
     - `score`, `reasoning`, `evidence_refs`
     - `related_docs` (list of doc refs/URLs)
     - `tags`, `generated_at`, `for_date`
   - If no hub, return minimal empty signal with `found=False`.

3. Wire existing knowledge-rag/graph call
   - Prefer existing method that lists/returns most-connected hub (MOC pattern).
   - If no direct method, add minimal adapter that calls `knowledge_rag.query_top_hub()` or equivalent.

4. Add minimal unit test for endpoint + service (param validation, 200/404/400).

5. Verify locally:
   - Start dev server.
   - `curl` endpoint with/without `for_date`.
   - Confirm JSON shape and that it surfaces a hub insight.

---

### Code snippets

#### 1) Route (FastAPI)

```python
# costinel/api/routes/cost_anomaly.py
from fastapi import APIRouter, HTTPException, Query
from datetime import date
from costinel.services.cost_anomaly_signal import CostAnomalySignalService
from costinel.schemas.cost_anomaly import TopHubSignalResponse, TopHubSignalNotFound

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])
signal_service = CostAnomalySignalService()

@router.get(
    "/signal/top-hub",
    response_model=TopHubSignalResponse | TopHubSignalNotFound,
    responses={
        200: {"model": TopHubSignalResponse},
        400: {"description": "Invalid parameters"},
        404: {"model": TopHubSignalNotFound, "description": "No hub signal found"},
    },
)
async def get_top_hub_signal(
    for_date: date | None = Query(
        None,
        description="Date to evaluate top hub signal (YYYY-MM-DD). Defaults to today UTC.",
        examples=["2026-04-27"],
    ),
):
    try:
        signal = signal_service.top_hub_signal(for_date=for_date)
        if not signal.found:
            raise HTTPException(status_code=404, detail="No top hub signal found for date")
        return signal
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

#### 2) Service

```python
# costinel/services/cost_anomaly_signal.py
from datetime import date, datetime
from typing import Any
from costinel.schemas.cost_anomaly import TopHubSignalResponse, HubSignal
from costinel.knowledge_rag import KnowledgeRAG  # existing module

class CostAnomalySignalService:
    def __init__(self):
        self.rag = KnowledgeRAG()

    def top_hub_signal(self, for_date: date | None = None) -> TopHubSignalResponse:
        target_date = for_date or date.today()
        # existing pattern: top-hub review (e.g., MOC)
        hub = self.rag.top_hub(for_date=target_date)  # expected: dict or None

        if not hub:
            return TopHubSignalResponse(
                found=False,
                for_date=target_date.isoformat(),
                generated_at=datetime.utcnow().isoformat() + "Z",
                signal=None,
            )

        signal = HubSignal(
            hub_id=hub.get("id") or hub.get("hub_id") or "unknown",
            hub_label=hub.get("label") or hub.get("name") or "Unknown Hub",
            hub_type=hub.get("type") or "hub",
            score=float(hub.get("score", 0)),
            reasoning=hub.get("reasoning") or hub.get("summary") or "",
            evidence_refs=hub.get("evidence_refs") or [],
            related_docs=hub.get("related_docs") or [],
            tags=hub.get("tags") or ["knowledge-rag", "graph", "top-hub"],
        )

        return TopHubSignalResponse(
            found=True,
            for_date=target_date.isoformat(),
            generated_at=datetime.utcnow().isoformat() + "Z",
            signal=signal,
        )
```

#### 3) Schemas

```python
# costinel/schemas/cost_anomaly.py
from pydantic import BaseModel
from typing import Optional, List

class HubSignal(BaseModel):
    hub_id: str
    hub_label: str
    hub_type: str
    score: float
    reasoning: str
    evidence_refs: List[str]
    related_docs: List[str]
    tags: List[str]

class TopHubSignalResponse(BaseModel):
    found: bool
    for_date: str
    generated_at: str
    signal: Optional[HubSignal] = None

class TopHubSignalNotFound(BaseModel):
    detail: str
```

#### 4) Minimal unit test (pytest)

```python
# tests/api/test_cost_anomaly_routes.py
from fastapi.testclient import TestClient
from costinel.main import app
from datetime import date

client = TestClient(app)

def test_get_top_hub_signal_default():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code in (200, 404)  # 404 ok if no hub for today
    data = resp.json()
    assert "found" in data
    assert "for_date" in data

def test_get_top_hub_signal_with_date():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub?for_date=2026-04-27")
    assert resp.status_code in (200, 404)

def test_get_top_hub_signal_invalid_date():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub?for_date=invalid")
    assert resp.status_code == 400
```

---

### Verification checklist

- [ ] Route registered in main router and exported in docs (if OpenAPI used).
- [ ] `knowledge_rag.top_hub(for_date)` exists or minimal adapter added.
- [ ] `curl` test:
  - `curl http://localhost:8000/api/v1/cost-anomaly/signal/top-hub`
  - `curl http://localhost:8000/api/v1/cost-anomaly/signal/top-hub?for_date=2026-04-27`
- [ ] Response shape matches `TopHubSignalResponse`.
- [ ] No execution side-effects (read-only).
- [ ] Unit tests pass.

---

**Tags:** #costinel #quality #api #knowledge-rag #graph #top-hub #sense-signal
