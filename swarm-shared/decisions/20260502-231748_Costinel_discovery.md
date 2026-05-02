# Costinel / discovery

## Final Implementation Plan — `GET /api/v1/cost-anomaly/signal/top-hub`

**Scope & constraints**
- Read-only, no side effects.
- Optional `?for_date=YYYY-MM-DD` (default today UTC) and `?top_n` (1–10, default 1).
- Reuse existing knowledge-graph assets (top-hub pattern) and project only `{hub_id, label, score, edges, context}`.
- Fast path: if knowledge-graph cache exists and is fresh (<5m), serve from cache; otherwise compute lightweight top-hub from anomaly/ops signals.
- No writes; no external mutations; no secrets in response.

**Files to touch (minimal)**
- `app/api/v1/endpoints/cost_anomaly.py` (or create if missing) — add route.
- `app/services/top_hub_service.py` — implement top-hub resolver.
- `app/core/cache.py` — add short-lived cache helper (or reuse existing).
- `tests/api/v1/test_cost_anomaly.py` — add read-only test.

**Implementation steps (≤2h)**

1. Add route `GET /api/v1/cost-anomaly/signal/top-hub`
   - Validate query params (`for_date`, `top_n`).
   - Call `TopHubService.get_top_hub(for_date, top_n)`.
   - Return `200` with payload:
     ```json
     {
       "request": { "for_date": "2026-05-02", "top_n": 1 },
       "generated_at": "2026-05-02T23:12:40Z",
       "top_hubs": [
         {
           "hub_id": "MOC",
           "label": "Mission Operations Center",
           "score": 0.92,
           "edges": 47,
           "context": "Highest betweenness in cost-anomaly signals; recent spikes in cross-account data-transfer costs."
         }
       ]
     }
     ```

2. Implement `TopHubService`
   - Try knowledge-rag cache first (`knowledge_rag.get_top_hub(for_date)`).
   - If cache miss, compute lightweight top-hub from recent anomaly/ops signals:
     - Load last 24h anomaly signals (or for `for_date`).
     - Build minimal adjacency on `hub_id` (from tags/labels in signals).
     - Score by weighted degree + recency.
     - Return top-N.
   - Keep compute bounded (<200ms) and memory-light.

3. Cache & observability
   - Cache key: `top_hub:{for_date}:{top_n}` with TTL 300s.
   - Log cache hit/miss and duration (info level).
   - No PII in logs.

4. Tests
   - Test param validation (invalid date, out-of-range top_n).
   - Test cache path and fallback path (mock service).
   - Ensure response shape matches schema.

**Code snippets**

`app/api/v1/endpoints/cost_anomaly.py`
```python
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator

from app.services.top_hub_service import TopHubService
from app.core.cache import get_cache

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

class TopHubRequest(BaseModel):
    for_date: Optional[str] = Field(None, description="YYYY-MM-DD (UTC)")
    top_n: int = Field(1, ge=1, le=10, description="Number of top hubs to return")

    @validator("for_date")
    def validate_date(cls, v):
        if v is None:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            raise ValueError("for_date must be YYYY-MM-DD")

class TopHubResponse(BaseModel):
    request: dict
    generated_at: str
    top_hubs: list[dict]

@router.get("/signal/top-hub", response_model=TopHubResponse)
async def get_top_hub(
    for_date: Optional[str] = Query(None, description="YYYY-MM-DD (UTC)"),
    top_n: int = Query(1, ge=1, le=10),
):
    req = TopHubRequest(for_date=for_date, top_n=top_n)
    cache_key = f"top_hub:{req.for_date}:{req.top_n}"
    cache = get_cache()

    cached = cache.get(cache_key) if cache else None
    if cached:
        return cached

    try:
        top_hubs = await TopHubService.get_top_hub(for_date=req.for_date, top_n=req.top_n)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to compute top hub: {exc}") from exc

    payload = TopHubResponse(
        request={"for_date": req.for_date, "top_n": req.top_n},
        generated_at=datetime.now(timezone.utc).isoformat(),
        top_hubs=top_hubs,
    ).dict()

    if cache:
        cache.set(cache_key, payload, ttl=300)
    return payload
```

`app/services/top_hub_service.py`
```python
from typing import List, Dict, Any
from datetime import datetime, timezone

# Optional: import existing knowledge-rag client if available
# from app.integrations.knowledge_rag import KnowledgeRAG

class TopHubService:
    """
    Lightweight top-hub resolver for Costinel.
    Preference: existing knowledge-rag cache -> fast in-memory fallback.
    """

    @classmethod
    async def get_top_hub(cls, for_date: str, top_n: int) -> List[Dict[str, Any]]:
        # 1) Try knowledge-rag cache (if available)
        # Uncomment and adapt if KnowledgeRAG exists:
        # try:
        #     cached = KnowledgeRAG.get_top_hub(for_date=for_date, top_n=top_n)
        #     if cached:
        #         return cls._normalize(cached)
        # except Exception:
        #     pass

        # 2) Fallback: lightweight compute from recent anomaly/ops signals
        return await cls._compute_top_hub(for_date=for_date, top_n=top_n)

    @classmethod
    async def _compute_top_hub(cls, for_date: str, top_n: int) -> List[Dict[str, Any]]:
        """
        Minimal fallback:
        - Loads recent anomaly/ops signals for `for_date` (or last 24h).
        - Projects hub_id from signal labels/tags.
        - Scores by weighted degree + recency.
        """
        # Placeholder loader — replace with actual signal store query.
        signals = await cls._load_signals(for_date=for_date)

        hub_scores: Dict[str, Dict[str, Any]] = {}
        for sig in signals:
            hub_id = sig.get("hub_id") or cls._extract_hub_id(sig)
            if not hub_id:
                continue
            weight = float(sig.get("severity", 1.0)) * float(sig.get("weight", 1.0))
            recency = cls._recency_factor(sig.get("timestamp"))
            score = weight * recency

            entry = hub_scores.setdefault(
                hub_id,
                {
                    "hub_id": hub_id,
                    "label": sig.get("hub_label") or hub_id,
                    "score": 0.0,
                    "edges": 0,
                    "context": "",
                },
            )
            entry["score"] += score
            entry["edges"] += 1

        # Build simple context from top signals per hub
        for hub_id, entry in hub_scores.items():
            top_ctx = sorted(
                [s for s in signals if cls._extract_hub_id(s) == hub_id],
                key=lambda s: float(s.get("severity", 0.0)),
                reverse=True,
            )[:3]
            ctx_parts = [s.get("summary") or s.get("description") or "" for s in top_ctx]
            entry["context"] = "; ".join(filter(None, ctx_parts))[:280]

        sorted_hubs = sorted(hub_scores.values(), key=lambda x: x["score"], reverse=True)[:top_n]
       
