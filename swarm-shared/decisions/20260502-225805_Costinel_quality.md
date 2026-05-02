# Costinel / quality

## Implementation Plan — Costinel Quality Increment (<2h)

**Scope (read-only, no infra):**  
Add `GET /api/v1/cost-anomaly/signal/top-hub` (optional `?for_date=YYYY-MM-DD`) that returns today’s strongest hub insight as a cost-anomaly signal (Sense + Signal; no Execute).

### Why this is highest-value (<2h)
- Directly applies **#knowledge-rag #graph #hub** pattern (top-hub doc insight) to Costinel.
- Read-only, no infra changes, no external calls during request (uses precomputed or local graph).
- Fits “Sense + Signal — ไม่ Execute” philosophy.
- Small surface: one route + one service + minimal tests/docs.

---

### Concrete steps (timeboxed)

1. **Add route** (`app/api/v1/endpoints/cost_anomaly.py` or equivalent)
   - `GET /api/v1/cost-anomaly/signal/top-hub`
   - Query param `for_date` (default: today UTC)
   - Response: 200 with signal payload; 404 if no hub; 400 on bad date.

2. **Add service** (`app/services/knowledge/top_hub.py`)
   - `get_top_hub_signal(for_date: date) -> TopHubSignal`
   - Uses local graph store or lightweight RAG lookup (e.g., reads from `knowledge/rag/top_hubs/YYYY-MM-DD.json` or queries an embedded graph index).
   - Projects fields: `hub_id`, `hub_name`, `strength`, `reason`, `related_docs`, `tags`, `date`.

3. **Add model/schema** (`app/schemas/cost_anomaly.py`)
   - `TopHubSignal` pydantic model (exclude_none, jsonable).

4. **Add minimal tests**
   - Route unit test (200/404/400).
   - Service unit test (mock graph).

5. **Add lightweight docs**
   - OpenAPI doc string on route.
   - One-line changelog entry.

6. **Verify & ship**
   - Run existing test suite.
   - Start dev server, hit endpoint, confirm JSON shape.

---

### Code snippets

#### 1) Schema (`app/schemas/cost_anomaly.py`)
```python
from datetime import date
from pydantic import BaseModel, Field
from typing import List, Optional

class RelatedDoc(BaseModel):
    doc_id: str
    title: str
    score: float

class TopHubSignal(BaseModel):
    date: date = Field(..., description="Date this signal applies to (YYYY-MM-DD)")
    hub_id: str = Field(..., description="Stable hub identifier (e.g., MOC)")
    hub_name: str = Field(..., description="Human-readable hub name")
    strength: float = Field(..., ge=0.0, le=1.0, description="Normalized strength of hub relevance")
    reason: str = Field(..., description="Short explanation why this hub is top for cost anomaly context")
    related_docs: List[RelatedDoc] = Field(default_factory=list, description="Top related docs from RAG")
    tags: List[str] = Field(default_factory=list, description="Tags (e.g., #knowledge-rag #graph #hub)")

    class Config:
        json_encoders = {
            date: lambda v: v.isoformat(),
        }
```

#### 2) Service (`app/services/knowledge/top_hub.py`)
```python
from datetime import date, datetime
from typing import Optional, List
from app.schemas.cost_anomaly import TopHubSignal, RelatedDoc

# Lightweight adapter: can be swapped for real graph/RAG calls later.
# For MVP, reads from knowledge/rag/top_hubs/YYYY-MM-DD.json if present,
# otherwise returns deterministic stub keyed by date (ensures reproducibility).

_KNOWLEDGE_TOP_HUBS_DIR = "knowledge/rag/top_hubs"

def _load_from_disk(for_date: date) -> Optional[dict]:
    import json, os
    p = os.path.join(_KNOWLEDGE_TOP_HUBS_DIR, f"{for_date.isoformat()}.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def _stub_signal(for_date: date) -> dict:
    # Deterministic stub keyed by date so behavior is stable in dev/test.
    # In production, replace with real graph/RAG lookup.
    weekday = for_date.weekday()
    hubs = [
        ("MOC", "Multi-Org Cost", 0.92),
        ("AWS-RI", "AWS Reserved Instances", 0.87),
        ("AZURE-VM", "Azure VM Efficiency", 0.81),
        ("GCP-COMMIT", "GCP Commitment Utilization", 0.78),
    ]
    hname, hlabel, hstr = hubs[weekday % len(hubs)]
    return {
        "date": for_date.isoformat(),
        "hub_id": hname,
        "hub_name": hlabel,
        "strength": hstr,
        "reason": f"Top-connected hub '{hlabel}' shows strongest contextual relevance to cost anomalies on {for_date.isoformat()}.",
        "related_docs": [
            {"doc_id": f"{hname}-overview", "title": f"{hlabel} Overview", "score": 0.95},
            {"doc_id": f"{hname}-anomalies", "title": f"{hlabel} Anomaly Patterns", "score": 0.88},
        ],
        "tags": ["#knowledge-rag", "#graph", "#hub"],
    }

def get_top_hub_signal(for_date: Optional[date] = None) -> TopHubSignal:
    """
    Return the top hub signal for a given date (default today UTC).
    Preference:
      1) Precomputed file in knowledge/rag/top_hubs/YYYY-MM-DD.json
      2) Deterministic stub (ensures dev/test stability)
    """
    if for_date is None:
        for_date = date.utcnow()

    data = _load_from_disk(for_date)
    if data is None:
        data = _stub_signal(for_date)

    data["related_docs"] = [RelatedDoc(**d) for d in data.get("related_docs", [])]
    return TopHubSignal(**data)
```

#### 3) Route (`app/api/v1/endpoints/cost_anomaly.py`)
```python
from fastapi import APIRouter, HTTPException, Query
from datetime import date
from app.services.knowledge.top_hub import get_top_hub_signal
from app.schemas.cost_anomaly import TopHubSignal

router = APIRouter()

@router.get("/cost-anomaly/signal/top-hub", response_model=TopHubSignal, tags=["cost-anomaly"])
async def get_top_hub_signal_endpoint(
    for_date: str = Query(None, description="Date in YYYY-MM-DD format (defaults to today UTC)")
):
    """
    Sense + Signal: return the top hub insight for cost anomaly context.
    No Execute — this is a signal for human review / downstream workflows.
    """
    try:
        if for_date is None:
            target = date.utcnow()
        else:
            target = date.fromisoformat(for_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid for_date format. Use YYYY-MM-DD.")

    try:
        signal = get_top_hub_signal(target)
    except Exception as exc:
        # Fail gracefully — surface 404 if no signal available
        raise HTTPException(status_code=404, detail=f"No top-hub signal available for {target.isoformat()}: {exc}") from exc

    return signal
```

#### 4) Minimal unit test (`tests/api/v1/test_cost_anomaly.py`)
```python
from fastapi.testclient import TestClient
from app.main import app
from datetime import date

client = TestClient(app)

def test_get_top_hub_signal_default():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert "hub_id" in data
    assert "strength" in data
    assert date.fromisoformat(data["date"])

def test_get_top_hub_signal_with_date():
    resp = client.get("/api/v1/c
