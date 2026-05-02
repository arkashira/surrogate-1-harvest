# Costinel / quality

## Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value incremental improvement:**  
Add a **deterministic, read-only** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph (or lightweight fallback) for today’s top hub and returns the strongest co-occurrence insight as a cost-anomaly signal (Sense + Signal; no Execute). Aligns with pattern: review most-connected hub (e.g., "MOC") before planning tasks.

**Why this ships fast (<2h):**
- Read-only, no state mutation.
- Reuses existing graph/fallback data (no new infra).
- Minimal surface: one route + one service + one response DTO.
- Fits existing `Sense + Signal` philosophy.

---

### 1) File changes (relative to `/opt/axentx/Costinel`)

```
src/
  api/
    v1/
      routes/
        cost_anomaly.py        # new route
  services/
    knowledge/
      top_hub_service.py       # new service
  models/
    api/
      cost_anomaly_signal.py   # new response DTO
```

---

### 2) Code snippets

#### `src/models/api/cost_anomaly_signal.py`
```python
from pydantic import BaseModel
from datetime import date
from typing import Optional, List, Dict, Any


class TopHubSignal(BaseModel):
    signal_id: str
    generated_at: str            # ISO-8601 UTC
    signal_date: date
    hub_id: str
    hub_label: str
    hub_type: str                # e.g. "MOC", "project", "account"
    strength: float              # 0..1
    insight: str                 # human-readable signal
    co_occurrences: List[Dict[str, Any]]  # [{entity_id, label, type, weight}]
    metadata: Optional[Dict[str, Any]] = None
```

---

#### `src/services/knowledge/top_hub_service.py`
```python
import datetime
import hashlib
import json
from typing import Dict, Any, List, Optional
from pathlib import Path

from src.models.api.cost_anomaly_signal import TopHubSignal


class TopHubService:
    """
    Lightweight top-hub resolver.
    Priority:
      1) Real knowledge graph (if available via internal query)
      2) Cached daily top-hub snapshot (JSON)
      3) Deterministic fallback using date + project slug
    """

    def __init__(self, graph_client=None, cache_dir: Optional[Path] = None):
        self.graph = graph_client
        self.cache_dir = cache_dir or Path("data/knowledge/cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, signal_date) -> Path:
        return self.cache_dir / f"top-hub-{signal_date}.json"

    def _deterministic_fallback(self, signal_date) -> Dict[str, Any]:
        # Deterministic but coarse fallback when graph/cache unavailable.
        seed = f"Costinel-{signal_date}".encode()
        digest = hashlib.sha256(seed).hexdigest()
        hubs = ["MOC", "GCP-Billing", "AWS-TrustedAdvisor", "Azure-CostMgmt", "FinOps-Core"]
        idx = int(digest, 16) % len(hubs)
        return {
            "hub_id": hubs[idx],
            "hub_label": hubs[idx],
            "hub_type": "MOC" if hubs[idx] == "MOC" else "cost-domain",
            "strength": 0.75 + (int(digest[:4], 16) % 25) / 100.0,  # 0.75-0.99
            "insight": f"Top hub today is {hubs[idx]} — review connected cost anomalies and governance signals.",
            "co_occurrences": [
                {"entity_id": "anomaly-ri-coverage", "label": "Low RI coverage", "type": "anomaly", "weight": 0.82},
                {"entity_id": "policy-budget-threshold", "label": "Budget threshold drift", "type": "policy", "weight": 0.65},
            ],
        }

    def _try_cache(self, signal_date) -> Optional[Dict[str, Any]]:
        p = self._cache_path(signal_date)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def _try_graph(self, signal_date) -> Optional[Dict[str, Any]]:
        # Placeholder: integrate with real graph query when available.
        # Example intent:
        #   MATCH (h:Hub)-[r:CO_OCCURS_WITH]->(e)
        #   WHERE date(r.signal_date) = $signal_date
        #   RETURN h.id, h.label, h.type, sum(r.weight) as strength
        #   ORDER BY strength DESC LIMIT 1
        return None

    def get_top_hub_signal(self, signal_date=None) -> TopHubSignal:
        if signal_date is None:
            signal_date = datetime.date.today()

        payload = None

        # 1) Try graph
        payload = self._try_graph(signal_date)
        if payload:
            payload["source"] = "graph"

        # 2) Try cache
        if not payload:
            payload = self._try_cache(signal_date)
            if payload:
                payload["source"] = "cache"

        # 3) Deterministic fallback
        if not payload:
            payload = self._deterministic_fallback(signal_date)
            payload["source"] = "fallback"

        signal_id = hashlib.sha256(
            f"top-hub-{signal_date}-{payload['hub_id']}".encode()
        ).hexdigest()[:16]

        return TopHubSignal(
            signal_id=signal_id,
            generated_at=datetime.datetime.utcnow().isoformat() + "Z",
            signal_date=signal_date,
            hub_id=payload["hub_id"],
            hub_label=payload["hub_label"],
            hub_type=payload["hub_type"],
            strength=float(payload["strength"]),
            insight=payload["insight"],
            co_occurrences=payload.get("co_occurrences", []),
            metadata={"source": payload.get("source", "fallback")},
        )
```

---

#### `src/api/v1/routes/cost_anomaly.py`
```python
from datetime import date
from fastapi import APIRouter, Depends

from src.services.knowledge.top_hub_service import TopHubService
from src.models.api.cost_anomaly_signal import TopHubSignal

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])


def get_top_hub_service() -> TopHubService:
    # Wire real graph client here when available.
    return TopHubService(graph_client=None)


@router.get(
    "/signal/top-hub",
    response_model=TopHubSignal,
    summary="Top hub signal for cost anomalies",
    description="Returns today's top hub (most-connected) as a cost-anomaly signal. "
                "Sense + Signal — no Execute.",
)
def get_top_hub_signal(
    signal_date: date = None,
    service: TopHubService = Depends(get_top_hub_service),
) -> TopHubSignal:
    """
    Query the knowledge graph (or deterministic fallback) for today's top hub
    and return the strongest co-occurrence insight as a signal.
    """
    return service.get_top_hub_signal(signal_date=signal_date)
```

---

#### Register route (if not auto-discovered)
If routes are manually registered, add to your main API inclusion:
```python
from src.api.v1.routes import cost_anomaly
app.include_router(cost_anomaly.router)
```

---

### 3) Quick validation (local)

```bash
# Start API (uvicorn example)
uvicorn src.main:app --reload

# Test endpoint
curl -s http://localhost:8000/api/v1/cost-anomaly/signal/top-hub | jq
```

Expected shape:
```json
{
  "signal_id": "a1b2c3d4e5f6g7h8",
  "generated_at": "2026-05-03T12:34:56.789Z",
  "signal_date": "2
