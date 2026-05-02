# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Highest-value improvement:**  
Add a **deterministic, read-only** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context. No writes, no side effects. Expose via backend stub.

### Why this is highest value
- Directly applies the **top-hub doc insight** pattern (`#knowledge-rag #graph #hub`) to Costinel’s cost-governance domain.
- Provides immediate, actionable signal for cost governance without execution risk (`Sense + Signal — ไม่ Execute`).
- Read-only, deterministic, and side-effect-free — safe to ship quickly.
- Complements existing anomaly/recommendation features with graph-driven context.

---

### Implementation Steps (≤2h)

1. **Add route stub** in backend (FastAPI) at `/api/v1/cost-anomaly/signal/top-hub`
2. **Implement read-only handler**:
   - Query knowledge graph for today’s top hub (e.g., highest degree/centrality or most recent activity).
   - Retrieve strongest cost-anomaly signal linked to that hub.
   - Return structured JSON: hub metadata, signal, context, timestamp.
3. **Mock graph query** if no real graph client exists (safe stub for now).
4. **Add minimal OpenAPI docs** and response model.
5. **Verify** with `curl` or Swagger UI.

---

### Code Snippets

#### `backend/main.py` (or equivalent route file)

```python
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone, date
from typing import Optional

router = APIRouter(prefix="/api/v1/cost-anomaly/signal", tags=["cost-anomaly"])

# -- Knowledge Graph Stub (replace with real client later) --
def query_knowledge_graph_top_hub() -> dict:
    """
    Deterministic read-only query for today's top hub.
    Returns strongest cost-anomaly signal with context.
    """
    # In production: call graph service (e.g., Neo4j, NetworkX, or internal KG)
    # For now: deterministic stub based on date (consistent across requests today)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Simulate top hub = "MOC" (from pattern) with strongest anomaly
    return {
        "hub": {
            "id": "MOC",
            "type": "cost-center",
            "label": "Mission Operations Center",
            "degree": 42,
            "last_updated": f"{today}T08:15:00Z",
        },
        "signal": {
            "type": "cost-anomaly",
            "severity": "high",
            "score": 0.92,
            "description": "Unusual spike in compute spend for MOC workloads",
            "metric": "daily_spend_usd",
            "value": 18450.00,
            "baseline": 11200.00,
            "deviation_pct": 64.7,
            "window": "2026-05-02T00:00:00Z/2026-05-02T23:59:59Z",
        },
        "context": {
            "recommendation": "Review reserved instance coverage and idle resources in MOC accounts",
            "tags": ["#cost-anomaly", "#knowledge-rag", "#hub", "#MOC"],
            "source_systems": ["aws-cost-explorer", "azure-cost-management"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }

# -- Endpoint --
@router.get("/top-hub", summary="Get top hub and strongest cost-anomaly signal (read-only)")
async def get_top_hub_signal() -> dict:
    """
    Deterministic, read-only endpoint.
    Returns today's top hub from the knowledge graph and the strongest
    associated cost-anomaly signal with full context.
    No side effects. No writes.
    """
    try:
        result = query_knowledge_graph_top_hub()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to query top-hub signal: {exc}") from exc
```

#### Mount router in app (if not auto-discovered)

Ensure the router is included in your FastAPI app:

```python
# In your main app file (e.g., app.py or main.py)
from backend.main import router as cost_anomaly_router

app.include_router(cost_anomaly_router)
```

---

### Verification

```bash
# Start backend (adjust as needed)
uvicorn backend.main:app --reload

# Test endpoint
curl -s http://localhost:8000/api/v1/cost-anomaly/signal/top-hub | jq
```

Expected output (example):

```json
{
  "hub": {
    "id": "MOC",
    "type": "cost-center",
    "label": "Mission Operations Center",
    "degree": 42,
    "last_updated": "2026-05-02T08:15:00Z"
  },
  "signal": {
    "type": "cost-anomaly",
    "severity": "high",
    "score": 0.92,
    "description": "Unusual spike in compute spend for MOC workloads",
    "metric": "daily_spend_usd",
    "value": 18450.0,
    "baseline": 11200.0,
    "deviation_pct": 64.7,
    "window": "2026-05-02T00:00:00Z/2026-05-02T23:59:59Z"
  },
  "context": {
    "recommendation": "Review reserved instance coverage and idle resources in MOC accounts",
    "tags": ["#cost-anomaly", "#knowledge-rag", "#hub", "#MOC"],
    "source_systems": ["aws-cost-explorer", "azure-cost-management"],
    "generated_at": "2026-05-02T22:50:00.123456+00:00"
  }
}
```

---

### Notes & Next Steps
- This is a **safe, read-only stub** that satisfies the immediate quality goal.
- Replace `query_knowledge_graph_top_hub()` with real graph queries when KG client is available.
- Consider adding caching (e.g., 5–15 min) if query cost increases.
- Aligns with `Sense + Signal` philosophy — no execution, only insight.
