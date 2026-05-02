# Costinel / discovery

## Implementation Plan — Costinel top-hub signal endpoint (<2h)

**Goal:** Add a read-only endpoint `GET /api/v1/cost-anomaly/signal/top-hub` (optional `?for_date=YYYY-MM-DD`) that returns the strongest knowledge-graph hub as a cost-anomaly signal (Sense + Signal, no execution).

**Why this is highest-value:**  
- Directly applies the “top-hub doc insight” pattern (#knowledge-rag #graph #hub) to Costinel ops.  
- Requires only a small API route + lightweight graph query + minimal frontend wiring.  
- Read-only, safe to ship, and immediately useful for anomaly triage.

---

### 1) File layout assumptions (based on repo root `/opt/axentx/Costinel`)
- FastAPI app in `app/main.py` or `app/api/routes.py`
- Knowledge graph access via a small service/module (e.g., `app/services/knowledge_rag.py`)
- Frontend (if any) in `app/frontend/` or simple HTML templates in `templates/`
- Tests in `tests/`

If layout differs, move files accordingly — logic remains identical.

---

### 2) Implementation steps (concrete)

#### A) Add knowledge-rag service helper
Create `app/services/knowledge_rag.py` (or extend existing):

```python
# app/services/knowledge_rag.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json
import os

# Lightweight in-memory graph for MVP; swap to Neo4j/NetworkX later if needed.
# Graph structure: {node_id: {label, type, edges: [{target, weight, relation}]}}

_KG_PATH = os.getenv("COSTINEL_KG_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "knowledge_graph.json"))

def _load_graph() -> Dict[str, Any]:
    if os.path.exists(_KG_PATH):
        with open(_KG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"nodes": {}, "edges": []}

def _hub_score(node_id: str, graph: Dict[str, Any]) -> float:
    """Simple weighted degree centrality."""
    node = graph["nodes"].get(node_id)
    if not node:
        return 0.0
    edges = node.get("edges", [])
    return float(sum(e.get("weight", 1.0) for e in edges))

def top_hub(for_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Return the strongest hub insight for an optional date.
    Returns:
      {
        "hub_id": str,
        "label": str,
        "type": str,
        "score": float,
        "edges": [...],
        "insight": str,
        "for_date": str | None,
        "ts": str
      }
    """
    graph = _load_graph()
    nodes = graph.get("nodes", {})
    if not nodes:
        return {
            "hub_id": "none",
            "label": "No data",
            "type": "unknown",
            "score": 0.0,
            "edges": [],
            "insight": "Knowledge graph is empty.",
            "for_date": for_date,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    # If date provided, prefer nodes tagged with that date (e.g., anomaly nodes).
    candidates = list(nodes.keys())
    if for_date:
        # naive tag filter: node metadata contains `date` or `for_date`
        tagged = [
            nid for nid in candidates
            if for_date in str(nodes[nid].get("date", "")) or for_date in str(nodes[nid].get("tags", []))
        ]
        if tagged:
            candidates = tagged

    best = max(candidates, key=lambda nid: _hub_score(nid, graph), default=None)
    if not best:
        return {
            "hub_id": "none",
            "label": "No hub",
            "type": "unknown",
            "score": 0.0,
            "edges": [],
            "insight": "No hub found for filters.",
            "for_date": for_date,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    node = nodes[best]
    edges = node.get("edges", [])
    top_edges = sorted(edges, key=lambda e: e.get("weight", 1.0), reverse=True)[:5]

    # Build a short actionable insight.
    if for_date:
        insight = f"Top hub '{node.get('label', best)}' on {for_date} indicates likely cost anomaly context."
    else:
        insight = f"Top hub '{node.get('label', best)}' indicates strongest contextual signal for cost anomalies."

    return {
        "hub_id": best,
        "label": node.get("label", best),
        "type": node.get("type", "hub"),
        "score": _hub_score(best, graph),
        "edges": top_edges,
        "insight": insight,
        "for_date": for_date,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
```

---

#### B) Add API route
Create or update route file, e.g. `app/api/routes.py` (or add to existing router):

```python
# app/api/routes.py
from fastapi import APIRouter, Query
from typing import Optional
from app.services.knowledge_rag import top_hub

router = APIRouter(prefix="/api/v1", tags=["cost-anomaly"])

@router.get("/cost-anomaly/signal/top-hub")
async def get_top_hub_signal(
    for_date: Optional[str] = Query(
        None,
        regex=r"^\d{4}-\d{2}-\d{2}$",
        description="Optional date filter (YYYY-MM-DD)"
    )
):
    """
    Return the strongest knowledge-graph hub as a cost-anomaly signal.
    Read-only. No execution.
    """
    return top_hub(for_date=for_date)
```

---

#### C) Mount router in main app
If not auto-discovered, mount in `app/main.py`:

```python
# app/main.py
from fastapi import FastAPI
from app.api.routes import router as api_router

app = FastAPI(title="Costinel", version="4.2.0")

app.include_router(api_router)

@app.get("/health")
async def health():
    return {"status": "ok"}
```

---

#### D) Minimal frontend/widget (optional)
If there’s a dashboard, add a small widget that calls `/api/v1/cost-anomaly/signal/top-hub` and renders `insight` + `score`. Example (plain HTML/JS):

```html
<!-- templates/top_hub_widget.html -->
<div id="top-hub-widget" class="card">
  <h3>Top Hub Signal</h3>
  <p id="hub-insight">Loading...</p>
  <small id="hub-ts"></small>
</div>

<script>
  async function loadTopHub(forDate) {
    const url = forDate ? `/api/v1/cost-anomaly/signal/top-hub?for_date=${forDate}` : `/api/v1/cost-anomaly/signal/top-hub`;
    try {
      const res = await fetch(url);
      const data = await res.json();
      document.getElementById('hub-insight').textContent = data.insight;
      document.getElementById('hub-ts').textContent = data.ts;
    } catch (e) {
      document.getElementById('hub-insight').textContent = "Unable to load hub signal.";
    }
  }
  loadTopHub();
</script>
```

---

#### E) Add a tiny sample graph for demo/testing
Create `app/data/knowledge_graph.json` (minimal):

```json
{
  "nodes": {
    "MOC": {
      "label": "MOC",
      "type": "process",
      "date": "2026-04-27",
      "tags": ["cost-anomaly", "governance"],
      "edges": [
        {"target": "ReservedInstance", "weight": 8.2, "relation": "drives"},
        {"target": "IdleResource", "weight": 7
