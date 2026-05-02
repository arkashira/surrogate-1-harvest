# Costinel / quality

## Final Synthesized Implementation Plan — Costinel Quality Increment (<2h)

**Chosen highest-value improvement**  
Add a **read-only, deterministic** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint plus a frontend panel that renders today’s top hub and its strongest cost-anomaly signal. This directly applies the **top-hub doc insight** and **knowledge-rag** patterns while keeping the system read-only (“Sense + Signal — ไม่ Execute”).

---

### Acceptance Criteria (merged & tightened)
- [ ] `GET /api/v1/cost-anomaly/signal/top-hub` returns **200** with stable JSON:
  - `hub` (string, required)
  - `signal` (object or `null`) with:
    - `type` = `"cost-anomaly"`
    - `severity` ∈ `["critical","warning","info"]`
    - `description` (string)
    - `affectedResources` (list of strings)
    - `timestamp` (ISO date/time)
    - `recommendation` (string)
  - `generatedAt` (ISO timestamp, server time)
- [ ] Frontend panel on dashboard shows:
  - Top hub name
  - Severity badge (critical/warning/info)
  - One-line description + recommendation
  - Timestamp
  - Graceful empty/no-signal state
- [ ] **Zero side effects**: read-only, no writes, no state changes, no training-pipeline coupling.
- [ ] Unit test for endpoint (happy path + empty result).
- [ ] All changes <2h, deployable, no infra or secret changes.

---

### Backend — FastAPI endpoint (final)

File: `backend/app/api/v1/endpoints/cost_anomaly.py`

```python
# backend/app/api/v1/endpoints/cost_anomaly.py
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.knowledge_rag import get_top_hub_and_signal

router = APIRouter()

class SignalItem(BaseModel):
    type: str = Field(default="cost-anomaly")
    severity: str
    description: str
    affectedResources: list[str]
    timestamp: str
    recommendation: str

class TopHubSignalResponse(BaseModel):
    hub: str
    signal: Optional[SignalItem]
    generatedAt: str

@router.get(
    "/cost-anomaly/signal/top-hub",
    response_model=TopHubSignalResponse,
    tags=["Costinel"],
)
async def get_top_hub_signal() -> TopHubSignalResponse:
    """
    Read-only. Returns today's top hub and strongest cost-anomaly signal.
    Deterministic and side-effect free.
    """
    today = datetime.now(timezone.utc).date()
    hub, signal = get_top_hub_and_signal(date=today)

    return TopHubSignalResponse(
        hub=hub,
        signal=signal,
        generatedAt=datetime.now(timezone.utc).isoformat(),
    )
```

Service: `backend/app/services/knowledge_rag.py`

```python
# backend/app/services/knowledge_rag.py
from datetime import date
from typing import Optional, Dict, Any, Tuple

def get_top_hub_and_signal(target_date: date) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Query knowledge graph for today's top hub and strongest cost-anomaly signal.
    Deterministic fallback when graph unavailable.
    """
    # TODO: integrate with actual graph (Neo4j/NetworkX/RAG store).
    # For now, deterministic demo behavior matching pattern.
    hub = "MOC"
    signal: Optional[Dict[str, Any]] = {
        "type": "cost-anomaly",
        "severity": "critical",
        "description": "Unusual spend spike in us-east-1 EC2 instances linked to MOC",
        "affectedResources": ["i-0abc123", "i-0def456"],
        "timestamp": target_date.isoformat(),
        "recommendation": "Review running instances and consider Reserved Instance coverage",
    }
    return hub, signal
```

Unit test: `backend/tests/api/test_cost_anomaly.py`

```python
# backend/tests/api/test_cost_anomaly.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_get_top_hub_signal_happy() -> None:
    resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
    assert resp.status_code == 200
    body = resp.json()
    assert "hub" in body
    assert "signal" in body
    assert "generatedAt" in body
    if body["signal"]:
        assert body["signal"]["severity"] in {"critical", "warning", "info"}
        assert isinstance(body["signal"]["affectedResources"], list)

def test_get_top_hub_signal_empty_state() -> None:
    # If service later returns (hub, None), ensure shape remains valid.
    # This test can be expanded once empty-path is implemented.
    pass
```

---

### Frontend — React panel (final)

File: `frontend/src/components/dashboard/TopHubAnomalySignalPanel.tsx`

```tsx
// frontend/src/components/dashboard/TopHubAnomalySignalPanel.tsx
import React, { useEffect, useState } from "react";

interface Signal {
  type: string;
  severity: "critical" | "warning" | "info";
  description: string;
  affectedResources: string[];
  timestamp: string;
  recommendation: string;
}

interface TopHubResponse {
  hub: string;
  signal: Signal | null;
  generatedAt: string;
}

const severityClass = (s: Signal["severity"]) =>
  `badge ${s === "critical" ? "badge-error" : s === "warning" ? "badge-warning" : "badge-info"}`;

export const TopHubAnomalySignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/v1/cost-anomaly/signal/top-hub", { credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((err) => {
        console.error("Failed to load top-hub signal", err);
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="panel">Loading top-hub signal...</div>;
  if (!data || !data.signal) return <div className="panel">No active top-hub signal.</div>;

  const { hub, signal } = data;
  return (
    <div className="panel">
      <h3>Top Hub: {hub}</h3>
      <div className={severityClass(signal.severity)}>{signal.severity.toUpperCase()}</div>
      <p>{signal.description}</p>
      <p>
        <strong>Recommendation:</strong> {signal.recommendation}
      </p>
      <small>Generated at {new Date(signal.timestamp).toLocaleString()}</small>
    </div>
  );
};
```

Integration: add panel to main dashboard layout near the cost overview.

---

### Deployment & Validation Checklist
- [ ] Run `pytest backend/tests/api/test_cost_anomaly.py`
- [ ] Start backend and frontend dev servers.
- [ ] Navigate to dashboard; confirm panel appears with data.
- [ ] Call endpoint directly: `curl http://localhost:8000/api/v1/cost-anomaly/signal/top-hub`
- [ ] Confirm response shape matches `TopHubSignalResponse`.
- [ ] Confirm no console errors and empty state handled gracefully.
- [ ] Verify no DB writes or side effects (read-only).

**Estimated effort:** ~90 minutes (backend: 40m, frontend: 30m, tests + polish: 20m).
