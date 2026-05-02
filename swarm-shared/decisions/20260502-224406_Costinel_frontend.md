# Costinel / frontend

## Final Synthesized Answer

**Chosen improvement:**  
Add a **deterministic, read-only** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost‑anomaly signal with full context. No writes, no side effects. Ship in <2h.

---

### Why this wins
- Directly applies the **top‑hub / knowledge‑graph** pattern before larger planning tasks.  
- Complements any existing `GET /api/v1/cost-anomaly/signal/top` with hub‑aware context.  
- Read‑only ⇒ safe, deterministic, and trivial to test/deploy.  
- Immediate frontend value: one call surfaces “most‑connected hub + strongest anomaly”.

---

### Implementation plan (≤2h)

| Step | Owner | Time | Action |
|------|-------|------|--------|
| 1 | Frontend | 20m | Add route constant and page/component (`/cost-anomaly/top-hub`) that calls new endpoint and renders signal card. |
| 2 | API | 30m | Add `GET /api/v1/cost-anomaly/signal/top-hub` (FastAPI). Query KG for today’s top hub and strongest cost‑anomaly signal. |
| 3 | Types | 10m | Add shared TypeScript interfaces (frontend) and Pydantic models (backend). |
| 4 | Tests | 20m | Minimal unit test for endpoint + frontend smoke test. |
| 5 | Docs | 10m | Update API docs and README section. |
| 6 | CI/CD | 10m | Ensure route is exposed and health‑check passes. |
| 7 | Deploy | 10m | Build and deploy to staging. |

---

### Shared types (single source of truth)

**Frontend (TypeScript)**
```ts
// src/lib/api/routes.ts
export const API_V1 = {
  COST_ANOMALY_SIGNAL_TOP: '/api/v1/cost-anomaly/signal/top',
  COST_ANOMALY_SIGNAL_TOP_HUB: '/api/v1/cost-anomaly/signal/top-hub',
} as const;
```

```ts
// src/lib/api/types.ts
export interface CostAnomalySignal {
  id: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  description: string;
  metric: string;
  value: number;
  baseline: number;
  deltaPercent: number;
  timestamp: string;
}

export interface TopHubAnomalyResponse {
  hub: {
    id: string;
    name: string;
    type: string;
    centrality: number;
  };
  signal: CostAnomalySignal;
  context: {
    summary: string;
    relatedDocs: Array<{ title: string; slug: string }>;
  };
  ts: string;
}
```

**Backend (Python/FastAPI)**
```python
# app/schemas/cost_anomaly.py
from pydantic import BaseModel
from typing import List

class CostAnomalySignal(BaseModel):
    id: str
    severity: str  # low|medium|high|critical
    description: str
    metric: str
    value: float
    baseline: float
    deltaPercent: float
    timestamp: str

class TopHubAnomalyResponse(BaseModel):
    hub: dict
    signal: CostAnomalySignal
    context: dict
    ts: str
```

---

### API endpoint (FastAPI)

`/opt/axentx/Costinel/app/api/v1/endpoints/cost_anomaly.py`
```python
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from app.schemas.cost_anomaly import TopHubAnomalyResponse
from app.services.knowledge_rag import query_top_hub_and_signal

router = APIRouter()

@router.get(
    "/api/v1/cost-anomaly/signal/top-hub",
    response_model=TopHubAnomalyResponse,
    summary="Get strongest cost-anomaly signal for today's top hub",
)
async def get_top_hub_anomaly_signal():
    """
    Deterministic, read-only.
    Queries the knowledge graph for today's top hub and the strongest
    cost-anomaly signal attached to it.
    """
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        result = await query_top_hub_and_signal(day=today)
        return {
            "hub": result["hub"],
            "signal": result["signal"],
            "context": result["context"],
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to fetch top-hub anomaly signal: {exc}",
        ) from exc
```

---

### Knowledge‑RAG service stub (backend)

`/opt/axentx/Costinel/app/services/knowledge_rag.py`
```python
from typing import Dict, Any

async def query_top_hub_and_signal(*, day: str) -> Dict[str, Any]:
    """
    TODO: integrate with knowledge-rag pipeline to resolve top hub
    (e.g., "MOC") and strongest cost-anomaly signal for `day`.

    Must remain read-only and deterministic.
    """
    # Placeholder until KG integration is wired.
    return {
        "hub": {
            "id": "MOC",
            "name": "MOC",
            "type": "cost-center",
            "centrality": 0.92,
        },
        "signal": {
            "id": "anom-20260503-001",
            "severity": "high",
            "description": "Unusual spike in data-transfer costs for MOC",
            "metric": "data-transfer-out",
            "value": 4820.5,
            "baseline": 2100.0,
            "deltaPercent": 129.55,
            "timestamp": f"{day}T14:22:00Z",
        },
        "context": {
            "summary": (
                "Top hub MOC shows 129% increase in outbound data transfer vs baseline. "
                "Likely due to cross-region replication burst."
            ),
            "relatedDocs": [
                {"title": "MOC Cost Governance Playbook", "slug": "moc-cost-playbook"},
                {"title": "Data Transfer Optimization Guide", "slug": "dt-optimization"},
            ],
        },
    }
```

---

### Frontend service and component

**Service**
```ts
// src/lib/api/costAnomaly.ts
import { API_V1 } from './routes';
import type { TopHubAnomalyResponse } from './types';

export async function fetchTopHubAnomalySignal(): Promise<TopHubAnomalyResponse> {
  const res = await fetch(API_V1.COST_ANOMALY_SIGNAL_TOP_HUB, {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  });

  if (!res.ok) {
    throw new Error(`Failed to fetch top-hub anomaly signal: ${res.status}`);
  }
  return res.json();
}
```

**Component**
```tsx
// src/components/CostAnomalyPanel.tsx
import { useEffect, useState } from 'react';
import { fetchTopHubAnomalySignal } from '../lib/api/costAnomaly';
import type { TopHubAnomalyResponse } from '../lib/api/types';

export function CostAnomalyPanel() {
  const [data, setData] = useState<TopHubAnomalyResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchTopHubAnomalySignal()
      .then(setData)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div>Loading top-hub signal…</div>;
  if (!data) return <div>No signal available</div>;

  return (
    <section>
      <h3>Top Hub: {data.hub.name}</h3>
      <p>Centrality: {data.hub.centrality.toFixed(2)}</p>
      <div>
        <strong>{
