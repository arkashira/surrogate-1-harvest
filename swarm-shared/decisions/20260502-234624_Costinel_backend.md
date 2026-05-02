# Costinel / backend

## Final Implementation Plan — Costinel Backend Top-Hub Signal (FastAPI)

**Scope**: Highest-value, read-only, <2h total  
**Principle**: “Sense + Signal — ไม่ Execute” (no self-execution)  
**Endpoint**: `GET /api/v1/cost-anomaly/signal/top-hub`  

**Deliverables** (merged strongest points):
- FastAPI endpoint with clean separation: router → service → model
- Lightweight in-memory/cached graph lookup (no DB writes, no external mutating calls)
- Structured response matching frontend expectations
- Health-check + observability (timing, counters, structured logging)
- Minimal dependencies; deterministic mock for dev; swap-in real graph query later

---

### 1) File changes (final paths)

Adopt Candidate 2’s clearer layout while keeping Candidate 1’s concise router style.

```
/opt/axentx/Costinel/
├── app/
│   ├── api/
│   │   ├── v1/
│   │   │   ├── endpoints/
│   │   │   │   └── cost_anomaly.py
│   │   │   └── __init__.py
│   ├── services/
│   │   └── top_hub_signal.py
│   ├── models/
│   │   └── signal.py
│   ├── core/
│   │   ├── config.py
│   │   └── logging.py
│   └── main.py
├── tests/
│   └── test_top_hub_signal.py
```

---

### 2) Models (`app/models/signal.py`)

```python
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class HubNode(BaseModel):
    id: str = Field(..., description="Hub identifier (e.g. MOC, AWS-EC2)")
    label: str = Field(..., description="Human-readable label")
    type: str = Field(..., description="Node type (service|account|region|tag)")
    centrality: float = Field(..., ge=0, le=1, description="Graph centrality score")
    risk_score: float = Field(..., ge=0, le=100, description="Anomaly risk score")
    cost_impact_usd: float = Field(..., description="Estimated cost impact (USD)")
    signals: List[str] = Field(default_factory=list, description="Anomaly signal keys")


class TopHubSignalResponse(BaseModel):
    generated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC generation timestamp"
    )
    window_start: datetime = Field(..., description="Analysis window start (UTC)")
    window_end: datetime = Field(..., description="Analysis window end (UTC)")
    top_hub: HubNode = Field(..., description="Most-connected / highest-risk hub")
    related_hubs: List[HubNode] = Field(
        default_factory=list,
        description="Secondary hubs (max 5)"
    )
    summary: str = Field(..., description="Short human-readable summary")
    actions: List[str] = Field(
        default_factory=list,
        description="Suggested human actions (non-executive)"
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Extra context (read-only)"
    )
```

---

### 3) Service (`app/services/top_hub_signal.py`)

```python
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Any
from functools import lru_cache
import logging

from app.models.signal import HubNode, TopHubSignalResponse

logger = logging.getLogger(__name__)


class TopHubSignalService:
    """
    Read-only signal generator.
    Deterministic-ish mock for dev; replace query_top_hub_graph() with real
    knowledge-rag graph query.
    """

    @staticmethod
    def _build_mock() -> TopHubSignalResponse:
        now = datetime.utcnow()
        window_start = now - timedelta(days=1)
        window_end = now

        top = HubNode(
            id="MOC",
            label="MOC (Mission-Oriented Compute)",
            type="tag",
            centrality=0.92,
            risk_score=87.4,
            cost_impact_usd=14230.50,
            signals=["spike-ec2", "idle-gpu", "unattached-eip"],
        )

        related = [
            HubNode(
                id="AWS-EC2",
                label="Amazon EC2",
                type="service",
                centrality=0.78,
                risk_score=72.1,
                cost_impact_usd=9820.00,
                signals=["ri-underutilized"],
            ),
            HubNode(
                id="GCP-ComputeEngine",
                label="GCP Compute Engine",
                type="service",
                centrality=0.65,
                risk_score=61.3,
                cost_impact_usd=5120.00,
                signals=["snapshot-retention"],
            ),
        ]

        summary = (
            "Top hub MOC shows elevated risk (87.4) driven by EC2 spikes and idle GPU resources. "
            "Estimated cost impact $14.2k in the last 24h. Review unattached EIPs and idle instances."
        )

        actions = [
            "Review unattached EIPs and schedule cleanup.",
            "Analyze idle GPU workloads for termination or scheduling.",
            "Validate Reserved Instance coverage for top EC2 spenders.",
            "Engage owners of MOC-tagged resources for optimization plan.",
        ]

        return TopHubSignalResponse(
            window_start=window_start,
            window_end=window_end,
            top_hub=top,
            related_hubs=related,
            summary=summary,
            actions=actions,
            metadata={"source": "knowledge-rag-graph", "mode": "read-only"},
        )

    @staticmethod
    @lru_cache(maxsize=1)
    def get_top_hub_signal_cached() -> TopHubSignalResponse:
        """
        Lightweight caching to avoid repeated heavy graph queries.
        Cache key is empty (static params); extend with window/type if needed.
        """
        try:
            # TODO: replace with real read-only graph query:
            # return TopHubSignalService.query_top_hub_graph(...)
            return TopHubSignalService._build_mock()
        except Exception as exc:
            logger.exception("Failed to generate top-hub signal")
            raise

    @staticmethod
    def query_top_hub_graph(
        window_days: int = 1,
        hub_type: Optional[str] = None,
        limit: int = 5,
    ) -> TopHubSignalResponse:
        """
        Placeholder for real knowledge-rag graph query.
        Must remain read-only; do not mutate state.
        """
        # Real implementation should:
        # - query top hub by centrality + risk_score
        # - include recent anomaly signals and cost impact
        # - return TopHubSignalResponse
        return TopHubSignalService._build_mock()
```

---

### 4) Endpoint (`app/api/v1/endpoints/cost_anomaly.py`)

```python
from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Dict, Any

from fastapi import APIRouter, HTTPException

from app.core.logging import get_logger
from app.models.signal import TopHubSignalResponse
from app.services.top_hub_signal import TopHubSignalService

logger = get_logger(__name__)

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


@router.get(
    "/signal/top-hub",
    response_model=TopHubSignalResponse,
    summary="Top-hub signal (read-only)",
    description=(
        "Returns the most-connected hub and related signals for cost anomalies. "
        "This endpoint is read-only and does not execute any changes (Sense + Signal)."
    ),
)
async def get_top_hub_signal() -> TopHubSignalResponse:
    """
    Sense + Signal endpoint: detect top hub and provide actionable context.
    No execution or mutation is performed.
    """
    start = perf_counter()
    try:
        payload = TopHubSignalService.get_top_hub_signal_cached()
        # Ensure generated_at reflects actual generation time
        payload.generated_at = datetime.utcnow()
        return payload
    except HTTPException:
        raise
   
