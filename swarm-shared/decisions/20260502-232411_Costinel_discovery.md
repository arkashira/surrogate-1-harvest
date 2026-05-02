# Costinel / discovery

## Final Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a **read-only, side-effect-free** `GET /api/v1/cost-anomaly/signal/top-hub` that surfaces the most-connected hub (deterministic fallback to `"MOC"` when graph unavailable) with contextual insights and actionable recommendations for governance workflows. Uses existing knowledge-rag graph; no writes; no state changes.

---

### 1) Concrete API contract (read-only)

```
GET /api/v1/cost-anomaly/signal/top-hub
Response 200
{
  "hub_id": "MOC",
  "label": "Mission Operations Center",
  "rank": 1,
  "score": 0.94,
  "connections": 127,
  "signals": [
    {
      "id": "anomaly-2026-04-27",
      "title": "CPU cost spike in MOC-linked cluster",
      "severity": "warning",
      "metric": "cpu_cost_usd",
      "value": 1240.50,
      "description": "72-hour rolling average exceeded baseline by 38%."
    }
  ],
  "context": [
    {
      "doc_id": "cost-governance-2026-04-27",
      "title": "Top-hub governance insight",
      "snippet": "Review the most-connected hub (e.g., MOC) before planning tasks.",
      "tags": ["#knowledge-rag", "#graph", "#hub"]
    }
  ],
  "recommendation": "Prioritize governance reviews and anomaly checks around MOC-linked resources.",
  "generated_at": "2026-05-03T14:03:00Z"
}
```

- **No mutations. No writes. No external state changes.**
- Deterministic fallback to `MOC` with empty signals/context when graph unavailable.

---

### 2) File layout (assumed)

```
/opt/axentx/Costinel/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── v1/
│   │   │   ├── endpoints/
│   │   │   │   └── cost_anomaly.py
│   │   │   └── __init__.py
│   ├── services/
│   │   ├── knowledge_rag.py
│   │   └── __init__.py
│   └── models/
│       └── signal.py
└── requirements.txt
```

---

### 3) Code snippets

#### `app/models/signal.py`

```python
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class Signal(BaseModel):
    id: str
    title: str
    severity: str  # info | warning | critical
    metric: Optional[str] = None
    value: Optional[float] = None
    description: str


class ContextDoc(BaseModel):
    doc_id: str
    title: str
    snippet: str
    tags: List[str]


class HubInsight(BaseModel):
    hub_id: str
    label: str
    rank: int
    score: float
    connections: int
    signals: List[Signal]
    context: List[ContextDoc]
    recommendation: str
    generated_at: datetime
```

---

#### `app/services/knowledge_rag.py`

```python
from typing import Dict, Any, List


class KnowledgeRAG:
    """
    Lightweight adapter around existing knowledge-rag graph.
    Assumes graph interface:
      - get_top_hubs(limit=1) -> [{"hub_id": "...", "label": "...", "connections": N, "score": float}]
      - get_related_docs(hub_id, limit=5) -> [{"doc_id": "...", "title": "...", "snippet": "...", "tags": [...], "signal": {...}}]
    """

    def __init__(self, graph_client=None):
        self.graph = graph_client  # inject or import existing client

    def top_hub_with_signals(self) -> Dict[str, Any]:
        try:
            hubs = self.graph.get_top_hubs(limit=1)
        except Exception:
            hubs = []

        if not hubs:
            # Deterministic fallback when graph empty/unavailable
            return {
                "hub_id": "MOC",
                "label": "Mission Operations Center",
                "rank": 1,
                "score": 0.0,
                "connections": 0,
                "signals": [],
                "context": [],
                "recommendation": "No graph data available; default governance hub (MOC).",
            }

        hub = hubs[0]
        try:
            docs = self.graph.get_related_docs(hub["hub_id"], limit=5)
        except Exception:
            docs = []

        signals = []
        context = []
        for d in docs:
            # Build context doc
            context.append(
                {
                    "doc_id": d.get("doc_id", d.get("title", "unknown")),
                    "title": d.get("title", "Untitled document"),
                    "snippet": d.get("snippet", d.get("content", "")[:256]),
                    "tags": d.get("tags", []),
                }
            )

            # Build signal if present
            payload = d.get("signal") or {}
            if payload:
                signals.append(
                    {
                        "id": payload.get("id", d.get("doc_id", "unknown")),
                        "title": payload.get("title", d.get("title", "Untitled signal")),
                        "severity": payload.get("severity", "info"),
                        "metric": payload.get("metric"),
                        "value": payload.get("value"),
                        "description": payload.get("description", d.get("snippet", "")[:512]),
                    }
                )

        return {
            "hub_id": hub["hub_id"],
            "label": hub["label"],
            "rank": 1,
            "score": float(hub.get("score", 0.94)),
            "connections": int(hub.get("connections", 0)),
            "signals": signals,
            "context": context,
            "recommendation": (
                "Prioritize governance reviews and anomaly checks around "
                f"{hub['label']}-linked resources."
            ),
        }
```

---

#### `app/api/v1/endpoints/cost_anomaly.py`

```python
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

from app.models.signal import HubInsight, Signal, ContextDoc
from app.services.knowledge_rag import KnowledgeRAG

router = APIRouter()


def get_rag() -> KnowledgeRAG:
    # Wire real graph client here (import from your existing module)
    return KnowledgeRAG(graph_client=None)


@router.get(
    "/cost-anomaly/signal/top-hub",
    response_model=HubInsight,
    summary="Top hub signal for cost governance discovery",
    tags=["cost-anomaly", "discovery"],
)
def get_top_hub_signal(rag: KnowledgeRAG = Depends(get_rag)) -> HubInsight:
    try:
        data = rag.top_hub_with_signals()
        return HubInsight(
            hub_id=data["hub_id"],
            label=data["label"],
            rank=data["rank"],
            score=data["score"],
            connections=data["connections"],
            signals=[Signal(**s) for s in data["signals"]],
            context=[ContextDoc(**c) for c in data["context"]],
            recommendation=data["recommendation"],
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        # Keep side-effect-free: no mutations, no external calls on failure
        raise HTTPException(status_code=500, detail=f"Failed to build signal: {exc}")
```

---

#### Wire into main app (`app/main.py`)

```python
from fastapi import FastAPI
from app.api.v1.endpoints import cost_anomaly

app = FastAPI(title="Costinel API", version="4.2.0")

app.include_router(cost_anomaly.router, prefix="/api/v1", tags=["v1"])
```

---

### 4) Quick
