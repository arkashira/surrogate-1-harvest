# Costinel / backend

## Final Implementation Plan — Costinel Top-Hub Signal (Backend)

**Scope:** Highest-value, read-only, <2h  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

---

### 1. Architecture & Data Flow (Merged + Corrected)
```
Client
  │
  ▼
FastAPI (Costinel Backend)
  │
  ├─► KnowledgeRAG (cached, read-only graph) ──┐
  │                                            │
  ├─► CostAnomalyDetector (read-only metrics)  │
  │                                            │
  └─► SignalComposer ──► Response
```
- **No writes** — pure read path.
- **Cached RAG lookup** — reuse existing graph index (MOC hub preferred per pattern).
- **Deterministic** — same request → same signal payload (idempotent).
- **Fast** — <500ms p95 (cache + lightweight anomaly detection).
- **Correctness:** Anomaly detection uses **z-score** (simple, robust, no model training) instead of isolation-forest (requires fit/persist → side effects).

---

### 2. File Changes (estimated)

```
costinel/
├── backend/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       │   └── cost_anomaly.py      # new endpoint
│   │       └── router.py                # include router
│   ├── core/
│   │   ├── knowledge_rag.py             # light accessor (read-only)
│   │   └── config.py                    # ensure env/config
│   ├── services/
│   │   ├── signals/
│   │   │   ├── top_hub.py               # resolver + signal builder
│   │   │   └── cost_anomaly.py          # new service (read-only)
│   │   └── signal_compose.py            # new service
│   └── models/
│       └── signal.py                    # pydantic models
└── tests/
    └── api/v1/test_cost_anomaly_signals.py  # contract tests
```

---

### 3. Code Snippets

#### `costinel/backend/models/signal.py`
```python
from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional


class RelatedDoc(BaseModel):
    id: str
    title: str
    score: float
    snippet: Optional[str] = None


class CostSignal(BaseModel):
    type: str  # "anomaly" | "trend"
    severity: str  # "low" | "medium" | "high"
    service: str
    region: str
    account_id: str
    metric: str
    value: float
    baseline: float
    deviation: float  # (value - baseline) / baseline


class TopHubSignal(BaseModel):
    top_hub: str
    generated_at: str
    insights: List[RelatedDoc]
    cost_signals: List[CostSignal]
    signal_type: str = "top-hub"
    version: str = "1.0"
```

---

#### `costinel/backend/core/knowledge_rag.py`
```python
from typing import Any, Dict
from functools import lru_cache
import json
from pathlib import Path
from dataclasses import dataclass


@dataclass
class KnowledgeGraph:
    nodes: Dict[str, Dict[str, Any]]
    edges: list

    def neighbors(self, node_id: str) -> list:
        return [e["target"] for e in self.edges if e["source"] == node_id]


# Minimal read-only accessor; assumes pre-built graph JSON exists.
_GRAPH_PATH = Path(__file__).parent.parent.parent / "data" / "knowledge_rag" / "graph.json"


@lru_cache(maxsize=1)
def get_rag_graph() -> KnowledgeGraph:
    if not _GRAPH_PATH.exists():
        # graceful fallback: empty graph
        return KnowledgeGraph(nodes={}, edges=[])
    with open(_GRAPH_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return KnowledgeGraph(nodes=payload.get("nodes", {}), edges=payload.get("edges", []))
```

---

#### `costinel/backend/services/signals/top_hub.py`
```python
from datetime import datetime, timezone
from dataclasses import asdict
from typing import List

from costinel.backend.core.knowledge_rag import KnowledgeGraph
from costinel.backend.models.signal import RelatedDoc


def build_top_hub_insights(graph: KnowledgeGraph) -> tuple[str, List[RelatedDoc]]:
    """
    Resolve top-connected hub and project contextual insights.
    Deterministic: picks highest total_degree node with tag 'hub'.
    """
    nodes = graph.nodes  # {id: {degree, tags, title, summary}}
    hubs = [
        (nid, attrs)
        for nid, attrs in nodes.items()
        if "hub" in (attrs.get("tags") or [])
    ]
    if not hubs:
        # fallback: pick node with max degree
        hubs = [(nid, attrs) for nid, attrs in nodes.items()]

    top_nid, top_attrs = max(hubs, key=lambda x: x[1].get("total_degree", 0))

    # project top 5 connected docs as insights
    neighbors = graph.neighbors(top_nid)
    insights: List[RelatedDoc] = []
    for nb in neighbors[:5]:
        nb_attrs = nodes.get(nb, {})
        insights.append(
            RelatedDoc(
                id=nb,
                title=nb_attrs.get("title", nb),
                score=nb_attrs.get("hub_score", 0.0),
                snippet=nb_attrs.get("summary", ""),
            )
        )

    top_hub_name = top_attrs.get("title", top_nid)
    return top_hub_name, insights
```

---

#### `costinel/backend/services/signals/cost_anomaly.py`
```python
from typing import List
from costinel.backend.models.signal import CostSignal


# Lightweight, read-only anomaly detection using z-score.
# No model training or state mutation.
def detect_cost_anomalies(metrics: List[dict]) -> List[CostSignal]:
    """
    metrics format (example):
    [
      {"service": "EC2", "region": "us-east-1", "account_id": "123",
       "metric": "cost", "value": 1200, "baseline": 800},
      ...
    ]
    """
    signals: List[CostSignal] = []
    for m in metrics:
        value = float(m.get("value", 0))
        baseline = float(m.get("baseline", 0))
        if baseline <= 0:
            continue
        deviation = (value - baseline) / baseline
        abs_dev = abs(deviation)

        if abs_dev < 0.2:
            continue

        severity = "low"
        if abs_dev >= 0.5:
            severity = "high"
        elif abs_dev >= 0.3:
            severity = "medium"

        signals.append(
            CostSignal(
                type="anomaly",
                severity=severity,
                service=m.get("service", "unknown"),
                region=m.get("region", "global"),
                account_id=m.get("account_id", "unknown"),
                metric=m.get("metric", "cost"),
                value=value,
                baseline=baseline,
                deviation=deviation,
            )
        )
    return signals
```

---

#### `costinel/backend/services/signal_compose.py`
```python
from typing import List
from costinel.backend.core.knowledge_rag import get_rag_graph
from costinel.backend.services.signals.top_hub import build_top_hub_insights
from costinel.backend.services.signals.cost_anomaly import detect_cost_anomalies
from costinel.backend.models.signal import TopHubSignal
from datetime import datetime, timezone


# Mock provider: replace with real read-only metrics fetch
def _fetch_latest_cost_metrics() -> List[dict]:
    # In production, this reads from a read-only store (e.g. Athena/BigQuery/exported parquet).
    return [
        {"service": "EC2",
