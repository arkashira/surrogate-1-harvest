# Costinel / quality

## Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value change:** Add a read-only endpoint `GET /api/v1/cost-anomaly/signal/top-hub` that surfaces today’s strongest hub insight from the knowledge graph as a cost-anomaly signal (Sense + Signal; no Execute). Aligns with pattern: top-hub doc insight + business research + knowledge-rag.

**Why this:**  
- Immediate value: surfaces the most-connected hub (e.g., "MOC") as a contextual signal for cost governance.  
- Read-only, no infra changes, fits <2h.  
- Reuses existing knowledge-rag/graph patterns and avoids execution actions (philosophy: Sense + Signal — ไม่ Execute).

---

### 1) Locate existing API surface (fast search)
```bash
grep -r "app\." /opt/axentx/Costinel --include="*.py" | head -20
grep -r "Blueprint" /opt/axentx/Costinel --include="*.py" | head -10
find /opt/axentx/Costinel -name "*.py" | head -30
```

If no results, check for FastAPI/Flask entrypoints:
```bash
grep -r "FastAPI\|flask\|@app.route" /opt/axentx/Costinel --include="*.py"
```

---

### 2) Create minimal endpoint (FastAPI example)

File: `/opt/axentx/Costinel/api/v1/cost_anomaly.py` (create path if missing)

```python
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])

# Lightweight stub for knowledge-rag hub lookup.
# Replace with real graph query when available.
def _get_top_hub_from_knowledge_rag(for_date: str) -> dict:
    """
    Simulate knowledge-rag top-hub lookup.
    Pattern: top-hub doc insight (2026-04-27) — review most-connected hub before planning.
    """
    # TODO: integrate real knowledge-rag / graph query here.
    return {
        "hub": "MOC",
        "score": 0.92,
        "reason": "Most-connected hub for cost governance signals on " + for_date,
        "context_links": [
            "/docs/knowledge-rag/moc",
            "/docs/cost-governance/top-hubs"
        ],
        "for_date": for_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signal_type": "top-hub",
        "action": "review",
        "severity": "info"
    }

@router.get("/signal/top-hub")
def get_top_hub_signal(
    for_date: Optional[str] = Query(
        None,
        description="Date to evaluate (YYYY-MM-DD). Defaults to today UTC.",
        regex=r"^\d{4}-\d{2}-\d{2}$"
    )
):
    """
    Sense + Signal: return today's top hub insight from knowledge graph.
    No execution. No state change.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_date = for_date or today_utc

    signal = _get_top_hub_from_knowledge_rag(target_date)
    return {
        "ok": True,
        "signal": signal,
        "meta": {
            "endpoint": "/api/v1/cost-anomaly/signal/top-hub",
            "philosophy": "Sense + Signal — ไม่ Execute",
            "tags": ["knowledge-rag", "graph", "hub", "cost-anomaly"]
        }
    }
```

---

### 3) Mount router in main app

If FastAPI app is in `/opt/axentx/Costinel/main.py` (or similar):

```python
# main.py (or wherever app is created)
from fastapi import FastAPI
from api.v1.cost_anomaly import router as cost_anomaly_router

app = FastAPI(title="Costinel", version="4.2.0")

app.include_router(cost_anomaly_router, prefix="/api/v1")
```

If project uses blueprints (Flask), convert to Flask route:

```python
# Flask variant (if applicable)
from flask import Blueprint, jsonify
from datetime import datetime, timezone

bp = Blueprint("cost_anomaly", __name__, url_prefix="/api/v1/cost-anomaly")

@bp.get("/signal/top-hub")
def top_hub_signal():
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    signal = {
        "hub": "MOC",
        "score": 0.92,
        "reason": "Most-connected hub for cost governance signals on " + today_utc,
        "context_links": ["/docs/knowledge-rag/moc"],
        "for_date": today_utc,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signal_type": "top-hub",
        "action": "review",
        "severity": "info"
    }
    return jsonify({"ok": True, "signal": signal})
```

Then register blueprint in app factory.

---

### 4) Quick local test

```bash
# If FastAPI
uvicorn main:app --reload --port 8000

# Test
curl "http://localhost:8000/api/v1/cost-anomaly/signal/top-hub?for_date=2026-05-02"
```

Expected:
```json
{
  "ok": true,
  "signal": {
    "hub": "MOC",
    "score": 0.92,
    "reason": "Most-connected hub for cost governance signals on 2026-05-02",
    "context_links": ["/docs/knowledge-rag/moc", "/docs/cost-governance/top-hubs"],
    "for_date": "2026-05-02",
    "generated_at": "...",
    "signal_type": "top-hub",
    "action": "review",
    "severity": "info"
  },
  "meta": {
    "endpoint": "/api/v1/cost-anomaly/signal/top-hub",
    "philosophy": "Sense + Signal — ไม่ Execute",
    "tags": ["knowledge-rag", "graph", "hub", "cost-anomaly"]
  }
}
```

---

### 5) Next steps (post-merge)
- Replace `_get_top_hub_from_knowledge_rag` with real graph query (knowledge-rag) once available.
- Add caching (short TTL) to avoid repeated heavy graph queries.
- Add monitoring/metrics for signal freshness and hub score drift.

**Estimated effort:** ~90 minutes (implementation + basic tests).
