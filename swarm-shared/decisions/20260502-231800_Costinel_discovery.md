# Costinel / discovery

## Final Implementation Plan — `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & constraints**
- Read-only, no side effects.
- Optional `?for_date=YYYY-MM-DD` (default today UTC) and `?top_n` (1–10, default 1).
- Reuse existing knowledge-graph assets (MOC-style hub ranking) to surface the most-connected hub for cost-anomaly signals.
- Return compact JSON for dashboard/alerting consumption.

**Why this is highest-value (<2h)**
- No infra changes; adds one endpoint + thin service layer.
- Immediately surfaces “top hub” insight (pattern: #knowledge-rag #graph #hub) for ops and anomaly review.
- Safe to ship: GET-only, no DB writes, no external calls during request.

---

### Implementation Steps (est. 60–90 min)

1. **Add route** (`app/api/v1/endpoints/cost_anomaly.py` or equivalent)
   - `GET /api/v1/cost-anomaly/signal/top-hub`
   - Validate query params with Pydantic.

2. **Service layer** (`app/services/cost_anomaly/top_hub.py`)
   - Load knowledge-graph hub ranking (cached file or in-memory graph).
   - If file-based: read precomputed `knowledge_rag/top_hubs/{for_date}.json` (or nearest date).
   - Compute top-N by connection score; fallback to global top hub if date missing.

3. **Response model**
   ```json
   {
     "date": "2026-05-02",
     "top_n": 1,
     "hubs": [
       {
         "hub_id": "MOC",
         "label": "Mission Operations Center",
         "score": 0.92,
         "signal_count": 14,
         "related_signals": [
           { "signal_id": "sig-01HV3X0Z...", "title": "EC2 spike in us-east-1", "severity": "high" }
         ]
       }
     ]
   }
   ```

4. **Caching & performance**
   - Cache precomputed daily top-hub payload for 5–15 min (in-memory or Redis if present).
   - Avoid graph recomputation per request.

5. **Tests**
   - One unit test for service ranking logic.
   - One endpoint test for 200 + schema.

6. **Deploy**
   - No migrations; restart not required if using dynamic import (or rolling restart for new route).

---

### Code snippets

**Route (FastAPI-style)**
```python
# app/api/v1/endpoints/cost_anomaly.py
from fastapi import APIRouter, Depends, Query
from app.schemas.cost_anomaly import TopHubResponse
from app.services.cost_anomaly.top_hub import get_top_hubs
from datetime import date

router = APIRouter()

@router.get("/cost-anomaly/signal/top-hub", response_model=TopHubResponse)
def get_top_hub_endpoint(
    for_date: date = Query(None, description="Date (YYYY-MM-DD), defaults to today UTC"),
    top_n: int = Query(1, ge=1, le=10, description="Number of top hubs to return")
):
    return get_top_hubs(for_date=for_date, top_n=top_n)
```

**Service**
```python
# app/services/cost_anomaly/top_hub.py
from datetime import date, datetime
from pathlib import Path
import json
from typing import List, Optional
from app.schemas.cost_anomaly import HubItem, RelatedSignal, TopHubResponse

KNOWLEDGE_RAG_DIR = Path(__file__).parent.parent.parent.parent / "knowledge_rag"

def _load_hubs_for_date(target_date: date) -> Optional[List[dict]]:
    file_path = KNOWLEDGE_RAG_DIR / "top_hubs" / f"{target_date.isoformat()}.json"
    if file_path.exists():
        with open(file_path) as f:
            return json.load(f)
    # fallback: latest available file
    files = sorted(KNOWLEDGE_RAG_DIR.glob("top_hubs/*.json"))
    if files:
        with open(files[-1]) as f:
            return json.load(f)
    return None

def get_top_hubs(for_date: Optional[date], top_n: int = 1) -> TopHubResponse:
    effective_date = for_date or date.today()
    hubs_data = _load_hubs_for_date(effective_date) or []

    # If file missing or empty, return minimal fallback
    if not hubs_data:
        return TopHubResponse(
            date=effective_date.isoformat(),
            top_n=top_n,
            hubs=[]
        )

    # Expect hubs_data as list of dicts with keys: hub_id, label, score, signal_count, related_signals
    top = sorted(hubs_data, key=lambda x: x.get("score", 0), reverse=True)[:top_n]

    hubs = []
    for item in top:
        related = [
            RelatedSignal(signal_id=s["signal_id"], title=s.get("title", ""), severity=s.get("severity", "medium"))
            for s in item.get("related_signals", [])
        ]
        hubs.append(
            HubItem(
                hub_id=item["hub_id"],
                label=item.get("label", item["hub_id"]),
                score=item.get("score", 0.0),
                signal_count=item.get("signal_count", 0),
                related_signals=related
            )
        )

    return TopHubResponse(
        date=effective_date.isoformat(),
        top_n=top_n,
        hubs=hubs
    )
```

**Schema**
```python
# app/schemas/cost_anomaly.py
from pydantic import BaseModel
from typing import List

class RelatedSignal(BaseModel):
    signal_id: str
    title: str
    severity: str = "medium"

class HubItem(BaseModel):
    hub_id: str
    label: str
    score: float
    signal_count: int
    related_signals: List[RelatedSignal] = []

class TopHubResponse(BaseModel):
    date: str
    top_n: int
    hubs: List[HubItem] = []
```

---

### Quick validation checklist
- [ ] Route registered in main API router.
- [ ] `knowledge_rag/top_hubs/YYYY-MM-DD.json` exists or fallback works.
- [ ] `GET /api/v1/cost-anomaly/signal/top-hub?top_n=1` returns 200 + schema.
- [ ] No DB writes; no external calls in request path.
- [ ] Cache layer added if repeated calls expected.

Ship this endpoint; it immediately enables dashboard/ops to surface the most-connected hub (e.g., MOC) for cost-anomaly signals — aligning with the #knowledge-rag #graph #hub pattern and requiring <2h.
