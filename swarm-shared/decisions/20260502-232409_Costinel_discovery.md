# Costinel / discovery

## Final Implementation Plan — Costinel Discovery: Top-Hub Signal Endpoint

**Scope (≤2h):**  
Expose a **read-only, side-effect-free** `GET /api/v1/cost-anomaly/signal/top-hub` that returns the most-connected hub and actionable governance insights. No mutations. No external writes. Fast, cacheable, observable.

---

### 1) Design Decisions (resolved)
- **Read-only & safe**: No writes, no state changes, no external mutations. Aligns with Costinel “Sense + Signal” philosophy.
- **Fast path**: Prefer precomputed artifact (`artifacts/top_hub.json`). Fallback to lightweight on-demand centrality only if graph is available in memory. Final fallback to static configurable hub.
- **Cacheable**: `Cache-Control: public, max-age=60` (configurable) to avoid hot loops.
- **Observability**: Structured logs + metrics (`counter`, `latency`, `error`).
- **Contract-first**: Stable JSON shape with explicit `type` (`computed` | `fallback`) and `ok` envelope.
- **Testability**: Dependency injection for service; unit tests mock service; integration test validates endpoint contract.

---

### 2) File Changes
- `app/api/v1/cost-anomaly/signal/top-hub/route.py` (new) — FastAPI route.
- `app/services/top_hub.py` (new) — service to compute/resolve top hub + insights.
- `app/core/config.py` (optional) — feature flag / cache TTL / fallback hub.
- `tests/api/v1/test_top_hub_signal.py` (new) — contract + unit tests.

---

### 3) Implementation Snippets

#### `app/services/top_hub.py`
```python
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class TopHubService:
    """
    Resolve the most-connected hub and contextual insights.
    Preference:
      1) Precomputed artifact (artifacts/top_hub.json)
      2) Lightweight on-demand centrality (if in-memory graph available)
      3) Static fallback (configurable)
    """

    FALLBACK_HUB = "MOC"
    FALLBACK_INSIGHTS = [
        "Most-connected governance node (fallback).",
        "Review ownership and policy exceptions before acting on signals.",
    ]

    def __init__(
        self,
        artifact_path: Optional[str] = None,
        fallback_hub: str = FALLBACK_HUB,
        fallback_insights: Optional[list[str]] = None,
    ) -> None:
        self.artifact_path = Path(artifact_path) if artifact_path else None
        self.fallback_hub = fallback_hub
        self.fallback_insights = fallback_insights or self.FALLBACK_INSIGHTS

    def get_top_hub(self) -> Dict[str, Any]:
        """
        Returns:
          {
            "hub": "<hub_id>",
            "score": <float>,
            "type": "computed" | "fallback",
            "insights": [<str>, ...],
            "context": { ... }
          }
        """
        # 1) Try precomputed artifact (fast, production-safe)
        artifact_result = self._from_artifact()
        if artifact_result:
            return artifact_result

        # 2) Try lightweight on-demand centrality (only if graph in memory)
        on_demand_result = self._from_on_demand()
        if on_demand_result:
            return on_demand_result

        # 3) Fallback
        return self._fallback()

    def _from_artifact(self) -> Optional[Dict[str, Any]]:
        if not self.artifact_path or not self.artifact_path.is_file():
            return None

        try:
            with self.artifact_path.open("r") as f:
                data = json.load(f)

            hub = data.get("hub", self.fallback_hub)
            return {
                "hub": hub,
                "score": float(data.get("score", 0.0)),
                "type": "computed",
                "insights": data.get("insights", self.fallback_insights),
                "context": data.get("context", {}),
            }
        except Exception as exc:
            logger.warning("Failed to load top-hub artifact %s: %s", self.artifact_path, exc, exc_info=True)
            return None

    def _from_on_demand(self) -> Optional[Dict[str, Any]]:
        try:
            import networkx as nx
        except Exception:
            logger.debug("networkx not available; skipping on-demand centrality")
            return None

        g = self._load_graph()
        if g is None or len(g) == 0:
            return None

        try:
            centrality = nx.degree_centrality(g)
            if not centrality:
                return None
            hub = max(centrality, key=centrality.get)
            return {
                "hub": hub,
                "score": centrality[hub],
                "type": "computed",
                "insights": [
                    f"Top hub by degree centrality ({len(g)} nodes).",
                    "High connectivity suggests central governance role — validate policies attached to this hub.",
                ],
                "context": {"node_count": len(g), "edge_count": g.size()},
            }
        except Exception as exc:
            logger.debug("On-demand centrality failed: %s", exc, exc_info=True)
            return None

    def _load_graph(self):
        # Placeholder: implement real graph loading (e.g., from artifact store or db)
        return None

    def _fallback(self) -> Dict[str, Any]:
        return {
            "hub": self.fallback_hub,
            "score": 0.0,
            "type": "fallback",
            "insights": self.fallback_insights,
            "context": {"note": "Using fallback top hub."},
        }
```

#### `app/api/v1/cost-anomaly/signal/top-hub/route.py`
```python
from fastapi import APIRouter, Depends, HTTPException
from app.services.top_hub import TopHubService
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


def get_top_hub_service() -> TopHubService:
    # Wire via DI/config in real app; keep simple for now
    return TopHubService(artifact_path=None)  # None -> fallback or on-demand


@router.get("/api/v1/cost-anomaly/signal/top-hub", response_model=Dict[str, Any])
def get_top_hub_signal(service: TopHubService = Depends(get_top_hub_service)) -> Dict[str, Any]:
    """
    Read-only signal endpoint.
    Returns the most-connected hub and contextual insights for governance review.
    """
    try:
        payload = service.get_top_hub()
        logger.info("Top hub signal: hub=%s type=%s", payload.get("hub"), payload.get("type"))
        return {
            "ok": True,
            "data": payload,
            "links": {
                "self": "/api/v1/cost-anomaly/signal/top-hub",
                "docs": "/docs#/Costinel/get_api_v1_cost_anomaly_signal_top_hub",
            },
        }
    except Exception as exc:
        logger.exception("Unexpected error in top-hub signal")
        raise HTTPException(status_code=500, detail="Unable to compute top-hub signal") from exc
```

#### `tests/api/v1/test_top_hub_signal.py`
```python
from fastapi.testclient import TestClient
from app.main import app  # adjust import to your app entrypoint
from app.services.top_hub import TopHubService

client = TestClient(app)


def test_top_hub_signal_returns_ok():
    # Patch service to avoid external deps in unit test
    class MockService:
        def get_top_hub(self):
            return {
                "hub": "MOC",
                "score": 0.92,
                "type": "fallback",
                "insights": ["test insight"],
                "context": {},
            }

    app.dependency_overrides[TopHubService] = lambda: MockService()
    try:
        resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body
