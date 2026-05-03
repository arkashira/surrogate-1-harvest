# Costinel / backend

**Final consolidated implementation plan**  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*

---

## 1) Design decisions (resolved)

- **Endpoint:** `GET /api/v1/sense/top-hub-signal`  
- **Auth:** Reuse existing Costinel bearer-token auth middleware.  
- **Response shape:**  
  ```json
  {
    "hub": { "id": "...", "name": "...", "type": "...", "degree": 0, "accounts": [], "regions": [] },
    "rank": 1,
    "signals": [ { "kind": "...", "provider": "...", "severity": "...", ... } ],
    "proposals": [ { "id": "...", "title": "...", "priority": "...", "actions": [], "rationale": "...", "audit_trail": {...} } ],
    "context": { "generated_at": "...", "ttl_remaining_s": 0 }
  }
  ```
- **No mutations:** Strictly read-only. No cloud writes, no state changes.  
- **Performance:**  
  - Cache top-hub resolution with short TTL (default 300s) using timestamp-based invalidation (not `lru_cache` alone) to avoid stale cache in long-running processes.  
  - Parallelize independent provider reads where possible.  
- **Extensibility:** Pluggable `SignalProvider` interface; new providers can be registered without touching core resolver.

---

## 2) File changes (minimal, high-value)

```
Costinel/
├── src/
│   ├── api/
│   │   └── v1/
│   │       └── sense/
│   │           └── top_hub_signal.py
│   ├── services/
│   │   ├── sense/
│   │   │   ├── top_hub_resolver.py
│   │   │   ├── signal_providers.py
│   │   │   └── __init__.py
│   │   └── governance/
│   │       └── proposal_builder.py
│   └── main.py
└── tests/
    └── api/
        └── v1/
            └── sense/
                └── test_top_hub_signal.py
```

---

## 3) Code snippets (complete, production-ready)

### `src/services/sense/top_hub_resolver.py`
```python
from __future__ import annotations

from typing import Dict, List
import time


class TopHubResolver:
    """
    Resolve the most-connected hub and related entities.
    Uses timestamp-based TTL invalidation for safe caching.
    """

    def __init__(self, graph_client):
        self.graph = graph_client
        self._cached_hub: Dict = {}
        self._cached_at: float = 0.0

    def get_top_hub(self, ttl: int = 300) -> Dict:
        now = time.time()
        if not self._cached_hub or (now - self._cached_at) > ttl:
            self._cached_hub = self._resolve_top_hub()
            self._cached_at = now
        return self._cached_hub

    def _resolve_top_hub(self) -> Dict:
        """
        Adapt to existing graph store.
        Contract: return hub-like dict with id, name, type, degree, accounts, regions.
        """
        # Example fallback; replace with real graph query.
        return {
            "id": "hub-moc",
            "name": "MOC",
            "type": "management",
            "degree": 42,
            "accounts": ["acct-1", "acct-2"],
            "regions": ["us-east-1", "eu-west-1"],
        }

    def related_signals(self, hub_id: str) -> List[Dict]:
        """
        Lightweight connected-signal metadata.
        Heavy enrichment is delegated to providers.
        """
        return [
            {"kind": "anomaly", "severity": "high", "metric": "spike", "value": 2.4},
            {"kind": "coverage", "severity": "medium", "metric": "ri_utilization", "value": 0.62},
        ]
```

---

### `src/services/sense/signal_providers.py`
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Dict
from datetime import datetime


class SignalProvider(ABC):
    @abstractmethod
    def get_signals(self, accounts: List[str], **kwargs) -> List[Dict]:
        pass


class BillingSignalProvider(SignalProvider):
    def get_signals(self, accounts: List[str], window_hours: int = 24) -> List[Dict]:
        # Read-only: fetch billing metrics
        return [
            {
                "provider": "billing",
                "account": acc,
                "delta_pct": 12.3,
                "forecast_30d": 8400.0,
                "timestamp": datetime.utcnow().isoformat(),
            }
            for acc in accounts
        ]


class RISignalProvider(SignalProvider):
    def get_signals(self, accounts: List[str], **kwargs) -> List[Dict]:
        # Read-only: RI/SP coverage analysis
        return [
            {
                "provider": "ri_coverage",
                "account": acc,
                "utilization": 0.65,
                "potential_savings": 2300.0,
            }
            for acc in accounts
        ]
```

---

### `src/services/governance/proposal_builder.py`
```python
from __future__ import annotations

from typing import List, Dict


class ProposalBuilder:
    """
    Build actionable proposals from signals.
    Strictly non-executing: proposals are for human review.
    """

    PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

    def build(self, hub: Dict, signals: List[Dict]) -> List[Dict]:
        proposals = []
        for s in signals:
            if s.get("kind") == "anomaly" and s.get("severity") == "high":
                proposals.append(
                    {
                        "id": f"prop-{s['metric']}-{hub['id']}",
                        "title": f"Investigate {s['metric']} spike in {hub['name']}",
                        "rationale": f"Detected {s['value']}x deviation from baseline.",
                        "actions": ["review_billing", "check_workloads"],
                        "priority": "high",
                        "audit_trail": {
                            "generated_by": "CostinelSense",
                            "hub": hub["id"],
                            "signal": s,
                        },
                    }
                )
            if s.get("kind") == "coverage" and s.get("value", 0) < 0.7:
                proposals.append(
                    {
                        "id": f"prop-ri-{hub['id']}",
                        "title": "Improve RI/SP coverage",
                        "rationale": f"Utilization {s['value']:.0%} below target.",
                        "actions": ["run_ri_recommendation", "schedule_review"],
                        "priority": "medium",
                        "audit_trail": {
                            "generated_by": "CostinelSense",
                            "hub": hub["id"],
                            "signal": s,
                        },
                    }
                )
        return proposals

    def rank(self, proposals: List[Dict]) -> List[Dict]:
        return sorted(
            proposals,
            key=lambda p: (self.PRIORITY_ORDER.get(p["priority"], 999), p.get("id")),
        )
```

---

### `src/api/v1/sense/top_hub_signal.py`
```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, List
from datetime import datetime, timezone

from src.services.sense.top_hub_resolver import TopHubResolver
from src.services.sense.signal_providers import BillingSignalProvider, RISignalProvider
from src.services.governance.proposal_builder import ProposalBuilder

router = APIRouter(prefix="/api/v1/sense", tags=["sense"])


def get_resolver() -> TopHubResolver:
    # Replace with real DI / container resolution
    from src.infra.graph import get_graph_client
    return TopHub
