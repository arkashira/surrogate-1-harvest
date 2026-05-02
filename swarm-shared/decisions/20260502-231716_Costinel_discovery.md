# Costinel / discovery

## Final Unified Implementation Plan  
`GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & constraints**  
- Read-only, no side effects, no DB writes, no external mutations.  
- Optional query params:  
  - `for_date` (YYYY‑MM‑DD, UTC, default today)  
  - `top_n` (1–10, default 1)  
- Reuse existing knowledge‑graph assets (top‑hub pattern) to surface the most‑connected hub(s) with cost‑anomaly context.  
- Fast path: pre‑computed hub rankings in `knowledge_rag/top_hub_cache.json` (updated nightly). Lightweight in‑memory fallback if cache missing.  
- Return compact JSON suitable for dashboard widgets and downstream signal pipelines.  
- Estimated effort: 60–90 minutes (code + tests + smoke).

---

### 1) Endpoint (FastAPI)

File: `app/api/v1/endpoints/cost_anomaly.py`

```python
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, conint

from app.services.knowledge_rag import get_top_hub_for_date

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


class TopHubSignal(BaseModel):
    hub_id: str = Field(..., description="Hub identifier (e.g. MOC)")
    hub_name: Optional[str] = None
    rank: int = Field(..., ge=1, description="Rank among hubs for the date")
    score: float = Field(..., description="Connection/strength score")
    for_date: str = Field(..., description="YYYY-MM-DD (UTC)")
    insights: List[str] = Field(default_factory=list, description="Contextual insights from RAG")
    related_docs: List[str] = Field(default_factory=list, description="Doc slugs or URIs")


@router.get("/signal/top-hub", response_model=List[TopHubSignal])
async def top_hub_signal(
    for_date: Optional[str] = Query(
        None,
        description="Date (YYYY-MM-DD) in UTC. Defaults to today.",
        regex=r"^\d{4}-\d{2}-\d{2}$",
    ),
    top_n: conint(ge=1, le=10) = Query(1, description="Number of top hubs to return"),
) -> List[TopHubSignal]:
    """
    Return top hub(s) for cost-anomaly signals on a given date using knowledge-graph insights.
    Read-only. No side effects.
    """
    try:
        target_date = (
            datetime.strptime(for_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if for_date
            else datetime.now(timezone.utc)
        )
        date_str = target_date.strftime("%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid for_date format. Use YYYY-MM-DD.") from exc

    results = get_top_hub_for_date(date_str, top_n=top_n)
    if not results:
        raise HTTPException(status_code=404, detail="No top-hub signal available for the requested date.")
    return results
```

---

### 2) Knowledge‑RAG service helper

File: `app/services/knowledge_rag.py`

```python
import json
from pathlib import Path
from typing import List, Dict, Any

from app.models.knowledge_rag import TopHubSignal

CACHE_PATH = Path(__file__).parent.parent.parent / "knowledge_rag" / "top_hub_cache.json"


def _load_cache() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_top_hub_for_date(date_str: str, top_n: int = 1) -> List[TopHubSignal]:
    """
    Return top hub(s) for date using pre-computed cache.

    Cache format:
    {
      "YYYY-MM-DD": [
        {
          "hub_id": "MOC",
          "hub_name": "Mission Operations Center",
          "rank": 1,
          "score": 0.92,
          "insights": [...],
          "related_docs": [...]
        }
      ]
    }

    If exact date missing, falls back to nearest earlier date (simple scan).
    """
    cache = _load_cache()
    entries = cache.get(date_str)

    # fallback to nearest previous date
    if not entries:
        dates = sorted(cache.keys())
        for d in reversed(dates):
            if d < date_str:
                entries = cache.get(d)
                break

    if not entries:
        return []

    results: List[TopHubSignal] = []
    for item in entries[:top_n]:
        results.append(
            TopHubSignal(
                hub_id=item["hub_id"],
                hub_name=item.get("hub_name"),
                rank=item.get("rank", 1),
                score=item.get("score", 0.0),
                for_date=date_str,
                insights=item.get("insights", []),
                related_docs=item.get("related_docs", []),
            )
        )
    return results
```

---

### 3) Pydantic model

File: `app/models/knowledge_rag.py`

```python
from pydantic import BaseModel, Field
from typing import List, Optional


class TopHubSignal(BaseModel):
    hub_id: str = Field(..., description="Hub identifier (e.g. MOC)")
    hub_name: Optional[str] = None
    rank: int = Field(..., ge=1, description="Rank among hubs for the date")
    score: float = Field(..., description="Connection/strength score")
    for_date: str = Field(..., description="YYYY-MM-DD (UTC)")
    insights: List[str] = Field(default_factory=list, description="Contextual insights from RAG")
    related_docs: List[str] = Field(default_factory=list, description="Doc slugs or URIs")
```

---

### 4) Wire into main API router

File: `app/api/v1/api.py`

```python
from fastapi import APIRouter

from app.api.v1.endpoints import cost_anomaly

api_router = APIRouter()
api_router.include_router(cost_anomaly.router, prefix="/v1", tags=["v1"])
```

(Adjust prefixing if your app already mounts `/api/v1` elsewhere.)

---

### 5) Cache bootstrap (one-time)

Create `knowledge_rag/top_hub_cache.json` so the endpoint works immediately:

```json
{
  "2026-05-02": [
    {
      "hub_id": "MOC",
      "hub_name": "Mission Operations Center",
      "rank": 1,
      "score": 0.92,
      "insights": [
        "MOC is the most-connected hub for cost-anomaly signals on 2026-05-02.",
        "High correlation with reserved instance coverage gaps in AP-Southeast-1.",
        "Recommended action: review RI purchase recommendations and idle resource signals."
      ],
      "related_docs": [
        "20260502-231240_Costinel_ops.md",
        "20260502-231520_Costinel_discovery.md"
      ]
    }
  ]
}
```

---

### 6) Tests (smoke)

File: `tests/api/v1/test_cost_anomaly.py`

```python
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_top_hub_signal_default():
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    if data:
        item = data[0]
        assert "hub_id" in item
        assert "score" in item
        assert item["rank"] >= 1


def test_top_hub_with_date():
    resp = client.get("/
