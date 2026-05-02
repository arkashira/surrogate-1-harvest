# Costinel / backend

Below is the **single, merged, final implementation plan** that keeps the highest-value parts from both candidates, removes duplication, resolves contradictions, and prioritizes **correctness + concrete actionability** while strictly preserving “Sense + Signal — ไม่ Execute” (read-only, no side effects).

---

## 1) Scope & Non-Goals
- **Scope**: Read-only signal endpoint, <2h implementation.
- **Non-Goals**:
  - No state mutation.
  - No external execution hooks or actuation.
  - No heavy ML/ETL pipelines (use lightweight heuristics; leave ML for future).

---

## 2) Endpoint Contract
- **Method**: `GET`
- **Path**: `/api/v1/cost-anomaly/signal/top-hub`
- **Query params (optional)**:
  - `top_n` (int, default=1) — number of top hubs to return.
  - `min_score` (float, default=0) — filter by hub score.
- **Responses**:
  - `200 OK` — `TopHubSignalResponse` (or list when `top_n>1`).
  - `204 No Content` — no hubs available (graceful degradation).
  - `422` — invalid query params.
  - `500` — internal error (logged + minimal safe payload).

---

## 3) File Layout
```
/opt/axentx/Costinel/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── v1/
│   │   │   ├── endpoints/
│   │   │   │   ├── cost_anomaly.py   ← endpoint here
│   │   │   │   └── __init__.py
│   │   │   └── __init__.py
│   │   └── __init__.py
│   ├── core/
│   │   ├── config.py
│   │   ├── graph.py                 ← lightweight hub graph
│   │   └── logging.py               ← structured logging helpers
│   ├── models/
│   │   └── signal.py                ← pydantic responses
│   └── services/
│       └── cost_signal.py           ← business logic
└── requirements.txt
```

---

## 4) Core Graph Utility (lightweight, deterministic)

`app/core/graph.py`
```python
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional


@dataclass
class HubNode:
    hub_id: str
    label: str
    connections: Set[str] = field(default_factory=set)
    metadata: Dict = field(default_factory=dict)


class CostHubGraph:
    """
    In-memory hub graph for 'Sense + Signal'.
    Populated from static file, DB, or seeded sample.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, HubNode] = {}
        self._hub_scores: Dict[str, float] = {}

    def add_link(self, a: str, b: str, meta: Dict | None = None) -> None:
        meta = meta or {}
        self.nodes.setdefault(a, HubNode(hub_id=a, label=a)).connections.add(b)
        self.nodes.setdefault(b, HubNode(hub_id=b, label=b)).connections.add(a)
        if meta:
            self.nodes[a].metadata.update(meta)
            self.nodes[b].metadata.update(meta)

    def compute_hub_scores(self, weights: Dict[str, float] | None = None) -> Dict[str, float]:
        """
        Degree centrality + metadata weights.
        Returns {hub_id: score}.
        """
        weights = weights or {"connection": 1.0, "cost_impact": 2.0, "anomaly_count": 3.0}
        scores: Dict[str, float] = {}

        for hid, node in self.nodes.items():
            base = len(node.connections) * weights.get("connection", 1.0)
            impact = float(node.metadata.get("cost_impact", 0)) * weights.get("cost_impact", 2.0)
            anomalies = float(node.metadata.get("anomaly_count", 0)) * weights.get("anomaly_count", 3.0)
            scores[hid] = round(base + impact + anomalies, 2)

        self._hub_scores = scores
        return scores

    def top_hubs(self, top_n: int = 1, min_score: float = 0) -> List[dict]:
        if not self._hub_scores:
            self.compute_hub_scores()
        if not self._hub_scores:
            return []

        sorted_hubs = sorted(self._hub_scores.items(), key=lambda kv: kv[1], reverse=True)
        results: List[dict] = []
        for hid, score in sorted_hubs:
            if score < min_score:
                continue
            node = self.nodes[hid]
            results.append(
                {
                    "hub_id": node.hub_id,
                    "label": node.label,
                    "score": score,
                    "connections": sorted(node.connections),
                    "metadata": node.metadata,
                }
            )
            if len(results) >= top_n:
                break
        return results

    @classmethod
    def sample(cls) -> "CostHubGraph":
        """
        Minimal seeded graph for immediate demo.
        Replace with file/DB loader in production.
        """
        g = cls()
        g.add_link("MOC", "AWS-Prod", {"cost_impact": 12000, "anomaly_count": 7})
        g.add_link("MOC", "GCP-Analytics", {"cost_impact": 8000, "anomaly_count": 4})
        g.add_link("MOC", "Azure-Infra", {"cost_impact": 9500, "anomaly_count": 5})
        g.add_link("MOC", "DataLake", {"cost_impact": 11000, "anomaly_count": 6})
        g.add_link("DataLake", "AWS-Prod", {"cost_impact": 6000, "anomaly_count": 2})
        g.add_link("CIAM", "Azure-Infra", {"cost_impact": 3000, "anomaly_count": 1})
        g.compute_hub_scores()
        return g
```

---

## 5) Pydantic Models

`app/models/signal.py`
```python
from __future__ import annotations

from pydantic import BaseModel
from typing import List, Optional, Dict, Any


class AffectedEntity(BaseModel):
    cloud: str
    account: str
    service: str
    estimated_monthly_impact_usd: float
    anomaly_count: int


class Signal(BaseModel):
    signal_id: str
    title: str
    severity: str  # low|medium|high|critical
    description: str
    recommendation: str


class TopHubSignalResponse(BaseModel):
    hub_id: str
    label: str
    hub_score: float
    summary: str
    affected_entities: List[AffectedEntity]
    signals: List[Signal]
    metadata: Optional[Dict[str, Any]] = None
```

---

## 6) Service Layer (Sense + Signal)

`app/services/cost_signal.py`
```python
from __future__ import annotations

from typing import List

from app.core.graph import CostHubGraph
from app.models.signal import AffectedEntity, Signal, TopHubSignalResponse


def build_top_hub_signals(
    graph: CostHubGraph | None = None, top_n: int = 1, min_score: float = 0
) -> List[TopHubSignalResponse]:
    """
    Sense + Signal: produce top-hub actionable signals without execution.
    """
    g = graph or CostHubGraph.sample()
    hubs = g.top_hubs(top_n=top_n, min_score=min_score)
    if not hubs:
        return []

    responses: List[TopHubSignalResponse] = []
    for top in hubs:
        entities: List[AffectedEntity] = []
        for conn in top.get("connections", []):
            cloud = "AWS" if "AWS" in conn else "GCP" if "GCP" in conn else "Azure" if "Azure" in conn else "Multi"
            account = conn
            service = conn.split("-")[-1] if "-" in conn else conn
            entities.append(
               
