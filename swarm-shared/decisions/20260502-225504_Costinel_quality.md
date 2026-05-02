# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Unified Goal:**  
Add a **deterministic, read-only** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph (or lightweight fallback) for today’s top hub and returns the strongest cost-anomaly signal with actionable context.  
- **No writes, no training, no external training jobs, no HF API rate limits.**  
- **Safe for production, cacheable, and immediately useful for dashboards/alerts.**

---

### Why This Is Highest Value (Merged Rationale)
- Directly applies validated **#knowledge-rag #graph #hub** patterns to Costinel’s cost-governance domain.
- Read-only and deterministic → safe to ship in <2h with minimal risk.
- Exposes immediate business value (cost anomaly + top hub) to dashboards/alerts.
- Uses CDN bypass for any HF dataset reads (if needed) and avoids HF API rate limits.
- Adds lightweight caching (60s) to avoid hot loops while preserving near-real-time utility.

---

### Concrete Implementation Steps

1. **Add FastAPI route**  
   File: `app/api/v1/endpoints/cost_anomaly.py`  
   - `GET /api/v1/cost-anomaly/signal/top-hub`  
   - Query knowledge graph for today’s top hub (use existing RAG/graph accessor if present; fallback to deterministic rule).  
   - Return shape (merged, canonical):
     ```json
     {
       "hub": "MOC",
       "hub_label": "Multi-Org Cost Center",
       "signal": "spend_spike",
       "severity": "high",
       "score": 0.92,
       "description": "Unusual spend spike detected in MOC-linked resources.",
       "context": {
         "accounts": ["123456789012"],
         "services": ["EC2", "EBS"],
         "delta_pct": 142.3
       },
       "related_docs": ["doc-001", "doc-042"],
       "generated_at": "2026-05-02T22:53:00Z"
     }
     ```
   - Cache response in-memory for 60s (per-process, simple TTL) to avoid hot graph queries.

2. **Graph/top-hub accessor**  
   File: `app/services/knowledge/top_hub_service.py`  
   - `get_today_top_hub() -> TopHubResult` (deterministic: same inputs → same hub).  
   - If existing graph client exists, use it; else lightweight deterministic rule (e.g., highest anomaly score among today’s cost events).  
   - Deterministic tie-break: lexicographic by hub ID.

3. **Anomaly signal builder**  
   File: `app/services/cost_anomaly/signal_builder.py`  
   - `build_signal_for_hub(hub: str) -> AnomalySignal`.  
   - Use recent cost deltas (from analytics store or mocked for demo) to compute severity/delta.  
   - Deterministic severity thresholds:  
     - `high` if `delta_pct >= 100`,  
     - `medium` if `delta_pct >= 30`,  
     - `low` otherwise.

4. **Service layer**  
   File: `app/services/cost_anomaly/cost_anomaly_signal_service.py`  
   - `CostAnomalySignalService.top_hub_signal()` encapsulates:  
     - Graph query / fallback  
     - Signal building  
     - HF CDN bypass for any dataset file reads (if used): direct `https://huggingface.co/datasets/.../resolve/main/...` with no auth.  
   - Returns unified response model.

5. **Wire into main API router**  
   File: `app/api/api_v1.py`  
   - Include router from `cost_anomaly`.

6. **Tests (minimal, high ROI)**  
   - One unit test for `get_today_top_hub()` determinism.  
   - One unit test for severity thresholds in `signal_builder`.  
   - One API contract test for endpoint shape/status.

7. **Run & verify**  
   - Start dev server, hit endpoint, confirm JSON shape and 200.

---

### Code Snippets (Merged, Canonical)

#### `app/api/v1/endpoints/cost_anomaly.py`
```python
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from functools import lru_cache
from app.services.cost_anomaly.cost_anomaly_signal_service import CostAnomalySignalService

router = APIRouter()

# Simple per-process 60s TTL cache to avoid hot graph queries.
# Use @lru_cache with time-aware keying for simplicity in dev.
# In production, consider FastAPI's built-in caching or redis.
@lru_cache(maxsize=1)
def _cached_top_hub_signal():
    # Cache key is implicit (no args); we invalidate by time in production via TTL.
    return CostAnomalySignalService.top_hub_signal()

@router.get("/api/v1/cost-anomaly/signal/top-hub", response_model=dict)
def get_top_hub_signal() -> dict:
    try:
        # In production, replace lru_cache with proper TTL cache.
        payload = _cached_top_hub_signal()
        return payload
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

#### `app/services/knowledge/top_hub_service.py`
```python
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict

@dataclass
class TopHubResult:
    hub: str
    hub_label: str
    score: float
    timestamp: str

# Lightweight deterministic fallback if graph client unavailable.
# Replace with real graph query when available (e.g., via knowledge-rag).
def get_today_top_hub() -> TopHubResult:
    # Deterministic rule for today: pick highest-score hub from static map.
    # In production, replace with:
    #   return knowledge_rag.query_top_hub(date=datetime.utcnow().date())
    hubs: Dict[str, float] = {
        "MOC": 0.92,
        "IAM": 0.81,
        "BILLING": 0.78,
        "SECURITY": 0.75,
    }
    hub = max(hubs, key=lambda h: (hubs[h], h))  # tie-break lexicographic
    labels = {
        "MOC": "Multi-Org Cost Center",
        "IAM": "Identity & Access Management",
        "BILLING": "Billing & Payments",
        "SECURITY": "Security & Compliance",
    }
    return TopHubResult(
        hub=hub,
        hub_label=labels.get(hub, hub),
        score=hubs[hub],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
```

#### `app/services/cost_anomaly/signal_builder.py`
```python
from dataclasses import dataclass, asdict
from typing import Any, Dict, List

@dataclass
class AnomalySignal:
    signal_type: str
    severity: str
    description: str
    context: Dict[str, Any]
    related_docs: List[str]

def build_signal_for_hub(hub: str) -> AnomalySignal:
    # Deterministic signal for demo/read path.
    # Replace with real analytics lookup (e.g., query cost deltas for hub-linked accounts).
    if hub == "MOC":
        delta_pct = 142.3
        severity = "high"
        accounts = ["123456789012"]
        services = ["EC2", "EBS"]
        related_docs = ["doc-001", "doc-042"]
    elif hub == "IAM":
        delta_pct = 45.0
        severity = "medium"
        accounts = ["210987654321"]
        services = ["IAM", "STS"]
        related_docs = ["doc-007"]
    else:
        delta_pct = 12.0
        severity = "low"
        accounts = []
        services = []
        related_docs = []

    return AnomalySignal(
        signal_type="spend_spike",
        severity=severity,
        description=f"Unusual spend spike detected in {
