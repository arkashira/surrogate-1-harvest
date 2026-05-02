# Costinel / discovery

## Final Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a read-only, side-effect-free `GET /api/v1/cost-anomaly/signal/top-hub` that uses existing knowledge-graph assets to return the top hub(s) for a requested date with a compact, actionable signal, clear context, and audit metadata. This enables dashboards and downstream processes to surface the top-hub insight immediately without recomputing heavy analytics.

---

### 1) Endpoint contract (canonical)

- **Method/Path**: `GET /api/v1/cost-anomaly/signal/top-hub`
- **Query params**:
  - `for_date` (optional, `YYYY-MM-DD`, default today UTC)
  - `top_n` (optional, integer 1–10, default 1)
- **Success response** (`200 OK`):
  ```json
  {
    "ok": true,
    "generated_at": "2026-05-02T23:15:00Z",
    "for_date": "2026-05-02",
    "top_n": 1,
    "signals": [
      {
        "rank": 1,
        "hub_id": "MOC",
        "hub_type": "anomaly",
        "score": 0.92,
        "title": "High-cost anomaly in MOC",
        "description": "MOC is the most-connected hub (47 nodes) and correlates with cost anomalies on 2026-05-02.",
        "actions": [
          {
            "label": "Review MOC cost breakdown",
            "url": "/cost/hubs/MOC?date=2026-05-02",
            "type": "dashboard"
          },
          {
            "label": "Check related services",
            "url": "/graph/hubs/MOC/connections?date=2026-05-02",
            "type": "graph"
          }
        ],
        "hub": {
          "hub_id": "MOC",
          "label": "MOC",
          "centrality_score": 0.92,
          "connected_nodes": 47,
          "top_connections": [
            { "target": "RI-Analyzer", "weight": 0.8 },
            { "target": "Forecast-Engine", "weight": 0.75 }
          ]
        }
      }
    ],
    "context": {
      "source_path": "data/knowledge_graph/2026-05-02/hubs.json",
      "read_only": true,
      "note": "Uses existing knowledge-graph assets; no recompute."
    }
  }
  ```
- **Not found / no data** (`200 OK` with empty signals):
  ```json
  {
    "ok": true,
    "generated_at": "...",
    "for_date": "2026-05-02",
    "top_n": 1,
    "signals": [],
    "context": {
      "source_path": "data/knowledge_graph/2026-05-02/hubs.json",
      "read_only": true,
      "note": "No hub data found for requested date."
    }
  }
  ```
- **Client error** (`400 Bad Request`): invalid `for_date` format or `top_n` out of range.

**Why this contract is best**
- Combines Candidate 1’s concrete hub structure with Candidate 2’s top-level `ok`, `signals[]`, and actionable `actions`.
- Uses `signals[]` (list) to support `top_n` cleanly and future extensibility.
- Includes audit/context (`generated_at`, `source_path`, `read_only`) for observability.
- Provides concrete `actions` so consumers can immediately navigate or investigate.

---

### 2) File structure (Costinel layout)

```
/opt/axentx/Costinel/
├── src/
│   ├── app.py
│   ├── api/
│   │   └── v1/
│   │       └── cost_anomaly/
│   │           └── signal.py          # endpoint
│   ├── services/
│   │   └── knowledge_graph.py         # KG reader (existing or new)
│   └── models/
│       └── signal.py                  # Pydantic models
├── data/
│   └── knowledge_graph/
│       └── {date}/
│           ├── hubs.json              # preferred
│           └── edges.json             # fallback
└── requirements.txt
```

---

### 3) Implementation steps

#### Step 1 — Response models (`src/models/signal.py`)
```python
from datetime import date
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field


class HubInsight(BaseModel):
    hub_id: str
    label: str
    centrality_score: float
    connected_nodes: int
    top_connections: List[Dict[str, Any]]


class SignalItem(BaseModel):
    rank: int
    hub_id: str
    hub_type: str = "anomaly"
    score: float
    title: str
    description: str
    actions: List[Dict[str, str]]
    hub: HubInsight


class TopHubSignalResponse(BaseModel):
    ok: bool = True
    generated_at: str  # ISO timestamp
    for_date: date
    top_n: int
    signals: List[SignalItem]
    context: Dict[str, Any]
```

#### Step 2 — Knowledge-graph reader (`src/services/knowledge_graph.py`)
```python
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import date

KG_ROOT = Path(__file__).parents[2] / "data" / "knowledge_graph"


def _load_hubs_for_date(target_date: date) -> Optional[Dict[str, Any]]:
    date_folder = KG_ROOT / str(target_date)
    hubs_file = date_folder / "hubs.json"

    if hubs_file.exists():
        with hubs_file.open() as f:
            return json.load(f)

    # Fallback: compute degree centrality from edges.json
    edges_file = date_folder / "edges.json"
    if not edges_file.exists():
        return None

    with edges_file.open() as f:
        edges = json.load(f)

    degree: Dict[str, int] = {}
    connections: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        src = e.get("source") or e.get("src")
        dst = e.get("target") or e.get("dst")
        if not src or not dst:
            continue
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1
        connections.setdefault(src, []).append({"target": dst, "weight": e.get("weight", 1)})
        connections.setdefault(dst, []).append({"target": src, "weight": e.get("weight", 1)})

    if not degree:
        return None

    # Build a simple hub-like payload for compatibility
    hubs = []
    for node, deg in degree.items():
        hubs.append({
            "hub_id": node,
            "label": str(node),
            "centrality_score": float(deg),
            "connected_nodes": deg,
            "top_connections": sorted(
                connections.get(node, []),
                key=lambda x: x.get("weight", 0),
                reverse=True,
            )[:5]
        })

    hubs.sort(key=lambda h: float(h["centrality_score"]), reverse=True)
    return {"hubs": hubs}


def get_top_hubs(target_date: date, top_n: int = 1) -> List[Dict[str, Any]]:
    """
    Return up to top_n hub dicts for target_date.
    """
    if top_n < 1:
        top_n = 1
    payload = _load_hubs_for_date(target_date)
    if not payload or not payload.get("hubs"):
        return []

    hubs = sorted(
        payload["hubs"],
        key=lambda h: float(h.get("centrality_score", 0)),
        reverse=True,
    )
    return hubs[:top_n]

