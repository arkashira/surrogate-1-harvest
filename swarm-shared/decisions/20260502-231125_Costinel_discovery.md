# Costinel / discovery

## Final Implementation Plan — `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & Constraints**  
- Read-only (Sense + Signal). No mutations.  
- Optional `?for_date=YYYY-MM-DD` (default today).  
- Reuse existing knowledge-graph snapshots; avoid recomputing graph metrics at runtime.  
- Return compact, stable JSON suitable for dashboards and alerting.  
- Estimated effort: **~90–110 min** (code + tests + docs + smoke).

---

### 1) API contract (merged & hardened)

**Endpoint**
```
GET /api/v1/cost-anomaly/signal/top-hub
```

**Query params**
- `for_date` (optional, `YYYY-MM-DD`, default today) — evaluation date.

**Success 200**
```json
{
  "meta": {
    "endpoint": "/api/v1/cost-anomaly/signal/top-hub",
    "for_date": "2026-05-03",
    "generated_at": "2026-05-03T14:23:00Z"
  },
  "signal": {
    "type": "cost_anomaly_top_hub",
    "hub": {
      "id": "MOC",
      "label": "MOC",
      "type": "service",
      "rank": 1,
      "centrality": {
        "degree": 42,
        "betweenness": 0.31,
        "eigenvector": 0.44,
        "composite_score": 0.92
      },
      "cost_context": {
        "daily_spend": 12840.50,
        "change_vs_prev_day_pct": 18.2,
        "anomaly_score": 0.73
      },
      "tags": ["knowledge-rag", "graph", "hub"]
    },
    "related_signals": [
      {
        "id": "ri_coverage_drop",
        "severity": "warning",
        "title": "RI coverage dropped 22% for MOC",
        "description": "On-demand share increased; projected cost delta +$2.3k/day"
      }
    ],
    "context_docs": [
      {
        "id": "doc-7f3a",
        "title": "Q1 Cloud Spend Drivers",
        "snippet": "Compute spikes in us-east-1 linked to MOC-related workloads...",
        "score": 0.87
      }
    ],
    "recommendation": "Review compute bursts tied to MOC workloads in us-east-1; consider RI/SP coverage for steady-state usage."
  },
  "audit": {
    "graph_version": "v2026-05-03-01",
    "data_sources": ["aws_cost_explorer", "knowledge_graph"]
  }
}
```

**Errors**
- 400 — invalid `for_date` format or out-of-range.  
- 404 — no graph snapshot for requested date.  
- 500 — internal error (include trace id).

**Why this shape wins**
- Keeps `meta.signal.audit` split for clarity (frontend expects `meta`, alerting expects `signal`, ops expects `audit`).  
- Combines centrality + cost context + related signals + docs + actionable recommendation (most complete, no contradictions).  
- `composite_score` enables ranking; `rank` is explicit.  
- `tags` and `type` preserved for filtering.

---

### 2) Implementation steps (concrete)

1. **Add route** in FastAPI router (`routes/cost_anomaly.py`).  
2. **Service layer** (`services/top_hub_signal.py`):
   - Validate `for_date`.  
   - Select latest graph snapshot ≤ `for_date` (from `knowledge/snapshots/` or parquet).  
   - Compute/read top hub by composite centrality (weighted: degree 0.4, betweenness 0.4, eigenvector 0.2).  
   - Enrich with daily cost aggregates (`cost_daily` table/parquet) and compute change vs prior day and anomaly score.  
   - Attach related signals (simple rule-based or from existing signals table).  
   - Attach top context docs (by node relevance/score from graph or vector store).  
   - Generate concise recommendation (template + light heuristics).  
   - Return `TopHubSignal` Pydantic model.
3. **Dependencies**: reuse existing `deps.py` for graph store / cost store.  
4. **Tests** (`tests/test_top_hub_signal.py`):
   - Invalid date → 400.  
   - Missing snapshot → 404.  
   - Happy path shape and required fields.  
   - Composite score ranking correctness.  
5. **OpenAPI docs**: include example response and error models.  
6. **Smoke test**: `uvicorn` + `curl` + validate JSON schema.

---

### 3) Code (merged best parts)

**Pydantic models**
```python
# models/top_hub_signal.py
from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import List, Optional

class Centrality(BaseModel):
    degree: int
    betweenness: float
    eigenvector: float
    composite_score: float = Field(..., ge=0.0, le=1.0)

class CostContext(BaseModel):
    daily_spend: float
    change_vs_prev_day_pct: float
    anomaly_score: float = Field(..., ge=0.0, le=1.0)

class RelatedSignal(BaseModel):
    id: str
    severity: str
    title: str
    description: str

class ContextDoc(BaseModel):
    id: str
    title: str
    snippet: str
    score: float = Field(..., ge=0.0, le=1.0)

class Hub(BaseModel):
    id: str
    label: str
    type: str
    rank: int = Field(..., ge=1)
    centrality: Centrality
    cost_context: CostContext
    tags: List[str] = []

class Signal(BaseModel):
    type: str = "cost_anomaly_top_hub"
    hub: Hub
    related_signals: List[RelatedSignal] = []
    context_docs: List[ContextDoc] = []
    recommendation: str

class TopHubSignal(BaseModel):
    meta: dict
    signal: Signal
    audit: dict
```

**Service**
```python
# services/top_hub_signal.py
import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from models.top_hub_signal import TopHubSignal, Hub, Centrality, CostContext, RelatedSignal, ContextDoc

SNAPSHOTS_DIR = Path("knowledge/snapshots")

def _latest_snapshot(on_or_before: date) -> Optional[Path]:
    candidates = [p for p in SNAPSHOTS_DIR.glob("*.json") if p.stem <= on_or_before.isoformat()]
    return max(candidates, key=lambda p: p.stem) if candidates else None

def _composite_score(centrality: dict) -> float:
    c = centrality
    return round(0.4 * c.get("degree", 0) + 0.4 * c.get("betweenness", 0.0) + 0.2 * c.get("eigenvector", 0.0), 4)

def get_top_hub_signal(for_date: Optional[date] = None) -> TopHubSignal:
    for_date = for_date or date.today()
    snapshot_path = _latest_snapshot(for_date)
    if not snapshot_path or not snapshot_path.exists():
        raise FileNotFoundError(f"No graph snapshot for {for_date}")

    with open(snapshot_path) as f:
        graph = json.load(f)

    nodes = graph.get("nodes", [])
    if not nodes:
        raise ValueError("Empty graph snapshot")

    # Rank by composite centrality
    scored = [(n, _composite_score(n.get("centrality", {}))) for n in nodes]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_node, top_score = scored[0]
    top_id = top_node["id"]

    # Cost context
    cost_daily = graph.get("cost_daily", {}).get(for_date.isoformat(), {}).get(top_id, {})
    prev_day =
