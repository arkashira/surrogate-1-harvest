# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value improvement:**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context. No writes, no side effects. Expose via backend stub.

### Why this is highest value
- Directly applies the **top-hub doc insight** pattern (review most-connected hub before planning).
- Provides immediate, actionable signal for cost governance without execution risk (`Sense + Signal — ไม่ Execute`).
- Read-only, deterministic, zero side effects — safe to ship and test quickly.
- Complements existing cost-anomaly and recommendation features.

---

### Concrete Implementation Steps (≤2h)

1. **Add backend route stub**  
   Create `backend/routes/cost_anomaly_top_hub.py` (or add to existing routes) with:
   - `GET /api/v1/cost-anomaly/signal/top-hub`
   - Query knowledge graph for today’s top hub (e.g., via `knowledge_rag` or graph query).
   - Return strongest cost-anomaly signal + context (hub name, score, related docs, timestamp).

2. **Integrate knowledge-rag query**  
   Use existing `knowledge_rag` tooling to:
   - Identify top hub for today (e.g., most-connected node in cost-anomaly graph).
   - Fetch strongest signal attached to that hub (e.g., highest anomaly score).

3. **Response schema**  
   JSON response:
   ```json
   {
     "ok": true,
     "data": {
       "hub": "MOC",
       "signal_type": "cost_spike",
       "severity": "high",
       "score": 0.92,
       "description": "Unusual spend spike in us-east-1 EC2",
       "affected_services": ["compute", "storage"],
       "recommendation": "Review reserved instance coverage and idle resources",
       "context_docs": ["doc-123", "doc-456"]
     },
     "context": {
       "source": "knowledge-rag",
       "read_only": true,
       "philosophy": "Sense + Signal — ไม่ Execute",
       "timestamp": "2026-05-03T10:00:00Z"
     }
   }
   ```

4. **Register route**  
   Add to FastAPI app (or existing backend router) in `backend/main.py` or equivalent.

5. **Test locally**  
   - Run backend.
   - `curl http://localhost:8000/api/v1/cost-anomaly/signal/top-hub`
   - Verify JSON response and 200 status.

6. **No frontend changes required** (read-only backend endpoint).

---

### Code Snippets

#### `backend/routes/cost_anomaly_top_hub.py`
```python
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from typing import List, Optional
import logging

# Assume knowledge_rag module exists with query_top_hub and get_signal_for_hub
# from services.knowledge_rag import query_top_hub, get_signal_for_hub

router = APIRouter()
logger = logging.getLogger(__name__)

# ---- Stubs for deterministic dev/test behavior ----
def query_top_hub(date: str) -> dict:
    """
    Stub: In production, this calls knowledge_rag to find the most-connected hub.
    Deterministic fallback for now: return "MOC" (from pattern).
    """
    return {"hub": "MOC", "score": 0.95}

def get_signal_for_hub(hub: str, date: str) -> dict:
    """
    Stub: Return a deterministic cost-anomaly signal for the hub.
    Replace with real graph query.
    """
    return {
        "signal_type": "cost_spike",
        "severity": "high",
        "score": 0.92,
        "description": f"Unusual cost spike detected in {hub} services",
        "affected_services": ["compute", "storage"],
        "recommendation": "Review reserved instance coverage and idle resources",
        "context_docs": ["doc-123", "doc-456"],
    }
# ---------------------------------------------------

@router.get("/api/v1/cost-anomaly/signal/top-hub")
async def get_top_hub_cost_anomaly_signal():
    """
    Deterministic, read-only endpoint.
    Returns the strongest cost-anomaly signal for today's top hub.
    No writes, no side effects.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        top_hub_info = query_top_hub(date=today)
        if not top_hub_info or "hub" not in top_hub_info:
            raise HTTPException(status_code=404, detail="Top hub not found")

        hub = top_hub_info["hub"]
        signal = get_signal_for_hub(hub=hub, date=today)

        return {
            "ok": True,
            "data": {
                "hub": hub,
                **signal,
            },
            "context": {
                "source": "knowledge-rag-stub",
                "read_only": True,
                "philosophy": "Sense + Signal — ไม่ Execute",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch top-hub cost anomaly signal")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
```

#### Register route in main app

```python
# backend/main.py (or wherever routes are registered)
from fastapi import FastAPI
from backend.routes.cost_anomaly_top_hub import router as cost_anomaly_top_hub_router

app = FastAPI(title="Costinel API", version="4.2.0")

app.include_router(cost_anomaly_top_hub_router)
```

#### Quick test script (optional)

```bash
# test_endpoint.sh
#!/usr/bin/env bash
set -euo pipefail

API_URL="http://localhost:8000/api/v1/cost-anomaly/signal/top-hub"
echo "Testing: $API_URL"
curl -s "$API_URL" | jq .
```

Make executable and run:

```bash
chmod +x test_endpoint.sh
./test_endpoint.sh
```

Expected output (shape):

```json
{
  "ok": true,
  "data": {
    "hub": "MOC",
    "signal_type": "cost_spike",
    "severity": "high",
    "score": 0.92,
    "description": "Unusual cost spike detected in MOC services",
    "affected_services": ["compute", "storage"],
    "recommendation": "Review reserved instance coverage and idle resources",
    "context_docs": ["doc-123", "doc-456"]
  },
  "context": {
    "source": "knowledge-rag-stub",
    "read_only": true,
    "philosophy": "Sense + Signal — ไม่ Execute",
    "timestamp": "2026-05-03T10:00:00.123456+00:00"
  }
}
```

---

### Verification Checklist

- [x] Route exists: `GET /api/v1/cost-anomaly/signal/top-hub`
- [x] Read-only (no POST/PUT/DELETE, no DB writes)
- [x] Returns top hub + strongest cost-anomaly signal
- [x] Includes context and timestamp
- [x] Deterministic stub for fast iteration
- [x] No side effects (safe to call repeatedly)

---

### Next Steps (post-ship)

- Replace stubs with real `knowledge_rag` graph queries.
- Add caching (TTL ~5m) to reduce graph load.
- Add monitoring/metrics for signal freshness and hub coverage.
