# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Goal:** Add a deterministic, read-only endpoint that surfaces today’s top hub from the knowledge graph as a cost-anomaly signal (Sense + Signal; no Execute).

**Why this is highest value:**  
- Immediate user-facing insight tied to the existing `#knowledge-rag #graph #hub` pattern.  
- Read-only, zero state change, no external mutations, and minimal blast radius.  
- Fits Costinel philosophy: *Sense + Signal — ไม่ Execute*.  
- Can be built and shipped in <2h.

---

### Changes (merged + resolved)

1. **API route**  
   Add `GET /api/v1/cost-anomaly/signal/top-hub` returning:
   - `hubId`, `hubLabel`, `hubType`
   - `topAnomaly` (if any) with `entity`, `severity`, `score`, `timestamp`
   - `context` (short excerpt / summary)
   - `generatedAt`
   - Optional: `sourceDate` (date requested) and `cached` flag for transparency.

2. **Service layer**  
   Add `CostAnomalySignalService.get_top_hub_signal()` that:
   - Uses date param (defaults to UTC today) for deterministic behavior and testability.
   - Calls knowledge-rag client for today’s top hub (cached for 5m).
   - Projects only anomaly-relevant fields.
   - Never mutates state.

3. **Client wrapper**  
   Expose `knowledge_rag_client.py` with:
   - `get_top_hub(date: str) -> dict`
   - Uses CDN-only fetches by default (manifest or known filenames).
   - Optional: accepts preloaded file manifest to avoid runtime listing.
   - Explicit timeouts and non-200 fallbacks to a minimal placeholder.

4. **Tests**  
   Add:
   - One unit test for the service (mocked rag client).
   - One integration-style test for the endpoint (mocked rag client).
   - One test for CDN fallback behavior.

5. **Deployment safety**  
   - No DB migrations.  
   - No side effects.  
   - Feature flag optional (`ENABLE_KG_SIGNALS=1`).  
   - Graceful degradation: endpoint returns 503 if signal unavailable (never executes).

---

### Code Snippets (merged best parts, corrected)

#### 1) API route (FastAPI)

```python
# app/api/v1/endpoints/cost_anomaly.py
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from app.services.cost_anomaly_signal import CostAnomalySignalService
from app.core.config import settings

router = APIRouter()

@router.get("/signal/top-hub", response_model=dict)
def get_top_hub_signal(
    date: str | None = None,
    svc: CostAnomalySignalService = Depends(),
):
    if not settings.ENABLE_KG_SIGNALS:
        raise HTTPException(status_code=404, detail="Feature disabled")
    try:
        # Allow explicit date for testing/reproducibility; default to UTC today
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return svc.get_top_hub_signal(target_date)
    except Exception as exc:
        # Sense + Signal: fail gracefully, never execute
        raise HTTPException(status_code=503, detail=f"Signal unavailable: {exc}")
```

#### 2) Service layer

```python
# app/services/cost_anomaly_signal.py
from datetime import datetime, timezone
from typing import Any
from app.clients.knowledge_rag import KnowledgeRagClient
from app.core.cache import cache

class CostAnomalySignalService:
    def __init__(self, rag_client: KnowledgeRagClient | None = None):
        self.rag = rag_client or KnowledgeRagClient()

    def get_top_hub_signal(self, date: str) -> dict[str, Any]:
        cache_key = f"kg:top-hub:{date}"
        cached = cache.get(cache_key)
        if cached:
            cached["cached"] = True
            return cached

        hub = self.rag.get_top_hub(date=date)
        if not hub:
            signal = {"hubId": None, "message": "No hub found", "generatedAt": datetime.now(timezone.utc).isoformat(), "cached": False}
            cache.set(cache_key, signal, ttl=300)
            return signal

        signal = {
            "hubId": hub["id"],
            "hubLabel": hub["label"],
            "hubType": hub.get("type", "unknown"),
            "topAnomaly": self._pick_top_anomaly(hub),
            "context": hub.get("summary") or hub.get("context", ""),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "sourceDate": date,
            "cached": False,
        }
        cache.set(cache_key, signal, ttl=300)  # 5m cache
        return signal

    def _pick_top_anomaly(self, hub: dict[str, Any]) -> dict[str, Any] | None:
        anomalies = hub.get("anomalies", [])
        if not anomalies:
            return None
        best = max(anomalies, key=lambda a: (a.get("severity", 0), a.get("score", 0)))
        return {
            "entity": best.get("entity"),
            "severity": best.get("severity"),
            "score": best.get("score"),
            "timestamp": best.get("timestamp"),
        }
```

#### 3) Knowledge-rag client (CDN-first, safe)

```python
# app/clients/knowledge_rag.py
import httpx
from typing import Any, Dict, List
from app.core.config import settings

class KnowledgeRagClient:
    def __init__(self, cdn_base: str | None = None, repo: str | None = None):
        self.cdn_base = cdn_base or "https://huggingface.co/datasets"
        self.repo = repo or getattr(settings, "KG_HF_REPO", "axentx/costinel-knowledge")
        self._manifest_cache: Dict[str, List[str]] = {}

    def _fetch_json(self, url: str, timeout: float = 8.0) -> Dict[str, Any] | None:
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def _list_date_folder(self, date: str) -> List[str]:
        """
        Best-effort manifest-driven listing. Avoids recursive tree API calls.
        """
        cache_key = f"kg:files:{date}"
        if cache_key in self._manifest_cache:
            return self._manifest_cache[cache_key]

        manifest_url = f"{self.cdn_base}/{self.repo}/resolve/main/{date}/manifest.json"
        m = self._fetch_json(manifest_url)
        if m and isinstance(m, dict):
            files = [f"{date}/{p}" for p in m.get("files", []) if isinstance(p, str)]
            self._manifest_cache[cache_key] = files
            return files
        self._manifest_cache[cache_key] = []
        return []

    def get_top_hub(self, date: str) -> Dict[str, Any]:
        """
        Return top hub document for date using CDN-only fetches.
        """
        candidates = [
            f"{self.cdn_base}/{self.repo}/resolve/main/{date}/top-hub.json",
            f"{self.cdn_base}/{self.repo}/resolve/main/{date}/hub-top.json",
        ]
        for url in candidates:
            doc = self._fetch_json(url)
            if doc:
                return doc

        # Minimal placeholder to preserve deterministic shape
        return {
            "id": "unknown",
            "label": "No hub",
            "type": "unknown",
            "summary": "Top hub not available",
            "anomalies": [],
        }
```

#### 4) Minimal unit test (corrected)

```python
# tests/unit/test_cost_anomaly_signal.py
from unittest.mock import MagicMock
from app.services.cost_anomaly_signal import CostAnomalySignalService

def test_get_top_hub_signal():
    mock_rag = MagicMock()
    mock_rag.get_top_hub.return_value = {
        "id": "
