# Costinel / backend

## Final Implementation — Costinel Top-Hub Signal (Backend)

**Scope:** Highest-value, read-only, <2h  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

---

### 1. Architecture & Data Flow

```
Client (frontend widget)
    ↓ HTTP GET /api/v1/cost-anomaly/signal/top-hub (auth via existing deps)
FastAPI route (read-only)
    ↓
TopHubSignalService
    ↓
GraphService (cached/precomputed graph)
    ↓
TopHubSignalDTO (response)
```

- **Read-only** — no writes, no external calls during request.
- **Fast** — p95 <100ms by using cached/precomputed graph.
- **Audit trail** — `computed_at`, `data_window`, `version`, `source`.
- **Auth** — reuse existing FastAPI dependency (e.g., `get_current_user` or API key) via route `dependencies=[...]`.
- **Observability** — structured logs + metrics (counter + histogram) for SLO/alerting.

---

### 2. File Changes

```
Costinel/
├── app/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       │   └── cost_anomaly.py        # new route
│   │       └── deps.py                   # auth/observability deps (reuse)
│   ├── core/
│   │   ├── config.py
│   │   └── logging.py                    # optional structured logging
│   ├── models/
│   │   └── signal.py                     # DB model (if persisting signals)
│   ├── schemas/
│   │   └── signal.py                     # Pydantic response schema
│   ├── services/
│   │   ├── graph.py                      # GraphService (cached, read-only)
│   │   └── signal/
│   │       └── top_hub.py                # TopHubSignalService
│   └── telemetry/
│       └── metrics.py                    # Prometheus/stats counters
└── tests/
    ├── unit/
    │   └── services/
    │       └── signal/
    │           └── test_top_hub.py
    └── api/
        └── v1/
            └── test_cost_anomaly_signal.py
```

---

### 3. Code Snippets

#### `app/schemas/signal.py`
```python
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class HubNode(BaseModel):
    hub_id: str = Field(..., description="Unique hub identifier")
    name: str = Field(..., description="Human-readable hub name")
    type: str = Field(..., description="Hub type (e.g., MOC, workload, account)")
    degree: int = Field(..., description="Number of connections (edges)")
    cost_impact: float = Field(..., description="Estimated cost impact (USD)")
    severity: str = Field(..., description="low|medium|high|critical")
    tags: List[str] = Field(default_factory=list)


class TopHubSignalDTO(BaseModel):
    signal_id: str = Field(..., description="Unique signal identifier")
    top_hub: Optional[HubNode] = Field(..., description="Most-connected hub (None if no data)")
    runner_ups: List[HubNode] = Field(default_factory=list, description="Next top N hubs")
    generated_at: datetime = Field(..., description="Signal generation timestamp (UTC)")
    data_window: dict = Field(..., description="Time window used for computation")
    version: str = Field("1.0", description="Signal schema/algorithm version")
    audit: dict = Field(default_factory=dict, description="Audit metadata")
```

---

#### `app/services/graph.py`
```python
from typing import List, Dict, Any
from app.schemas.signal import HubNode


class GraphService:
    """
    Read-only graph service.
    In production, this reads from a cache or precomputed table.
    No mutations or external calls during request.
    """

    def __init__(self):
        self._graph: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def load_from_cache(self) -> None:
        if self._loaded:
            return
        # Replace with real cache/db read (e.g., Redis, materialized view)
        self._graph = {
            "MOC": {
                "name": "Mission Operations Center",
                "type": "MOC",
                "degree": 42,
                "cost_impact": 18430.50,
                "severity": "critical",
                "tags": ["central", "real-time", "compliance"],
                "connections": ["prod-eks-us", "prod-eks-eu", "data-lake", "billing"],
            },
            "prod-eks-us": {
                "name": "Production EKS US",
                "type": "workload",
                "degree": 18,
                "cost_impact": 9200.00,
                "severity": "high",
                "tags": ["k8s", "us-east-1"],
                "connections": ["MOC", "data-lake"],
            },
            "prod-eks-eu": {
                "name": "Production EKS EU",
                "type": "workload",
                "degree": 15,
                "cost_impact": 7100.00,
                "severity": "high",
                "tags": ["k8s", "eu-west-1"],
                "connections": ["MOC"],
            },
        }
        self._loaded = True

    def get_top_hubs(self, limit: int = 5) -> List[HubNode]:
        self.load_from_cache()
        nodes = [
            HubNode(
                hub_id=hid,
                name=info["name"],
                type=info["type"],
                degree=info["degree"],
                cost_impact=info["cost_impact"],
                severity=info["severity"],
                tags=info["tags"],
            )
            for hid, info in self._graph.items()
        ]
        nodes.sort(key=lambda n: (-n.degree, -n.cost_impact))
        return nodes[:limit]
```

---

#### `app/services/signal/top_hub.py`
```python
import uuid
from datetime import datetime, timezone
from typing import Dict

from app.schemas.signal import TopHubSignalDTO
from app.services.graph import GraphService


class TopHubSignalService:
    def __init__(self, graph_service: GraphService):
        self.graph_service = graph_service

    def get_top_hub_signal(self) -> TopHubSignalDTO:
        top_hubs = self.graph_service.get_top_hubs(limit=5)
        now = datetime.now(timezone.utc)

        if not top_hubs:
            return TopHubSignalDTO(
                signal_id=str(uuid.uuid4()),
                top_hub=None,
                runner_ups=[],
                generated_at=now,
                data_window={"start": None, "end": None},
                version="1.0",
                audit={"source": "graph_service", "note": "no_hubs_found"},
            )

        top = top_hubs[0]
        runner_ups = top_hubs[1:]

        return TopHubSignalDTO(
            signal_id=str(uuid.uuid4()),
            top_hub=top,
            runner_ups=runner_ups,
            generated_at=now,
            data_window={
                "start": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                "end": now.isoformat(),
            },
            version="1.0",
            audit={
                "source": "graph_service",
                "computed_by": "TopHubSignalService",
                "read_only": True,
            },
        )
```

---

#### `app/api/v1/endpoints/cost_anomaly.py`
```python
from fastapi import APIRouter, Depends

from app.schemas.signal import TopHubSignalDTO
from app.services.graph import GraphService
from app.services.signal.top_hub import TopHubSignalService
from app.telemetry.metrics import REQUEST
