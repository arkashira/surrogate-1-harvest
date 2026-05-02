# Costinel / frontend

## Final Synthesis — Best Parts + Corrected + Actionable

**Chosen improvement**  
Add a **read-only**, deterministic  
`GET /api/v1/cost-anomaly/signal/top-hub`  
that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context.  
- Zero writes, zero side effects.  
- Aligns with “Sense + Signal — ไม่ Execute”.  
- Directly enables dashboard to surface the most-connected anomaly.

---

### 1) API contract (single source of truth)

```ts
// src/lib/api/routes.ts
export const API_V1 = {
  COST_ANOMALY_TOP_HUB: '/api/v1/cost-anomaly/signal/top-hub',
} as const;
```

```ts
// src/lib/api/types.ts
export interface TopHubCostAnomalySignal {
  hubId: string;
  hubName: string;
  hubType: 'cost-anomaly' | 'governance' | 'insight';
  signalId: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  description: string;
  context: Record<string, unknown>;
  score: number;
  timestamp: string; // ISO
  source: string;
}
```

---

### 2) Backend endpoint (FastAPI) — minimal, correct, testable

```python
# app/routes/cost_anomaly.py
from datetime import datetime, timezone
from fastapi import APIRouter
from services.knowledge_rag import get_top_hub_and_signal

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

@router.get("/signal/top-hub")
async def get_top_hub_signal() -> dict:
    """
    Deterministic read-only endpoint.
    Returns today's top hub and strongest cost-anomaly signal.
    """
    try:
        result = get_top_hub_and_signal(as_of=datetime.now(timezone.utc).date())
        # Normalize shape to match frontend contract
        return {
            "hubId": result["hub"]["id"],
            "hubName": result["hub"]["name"],
            "hubType": result["hub"]["type"],
            "signalId": result["signal"]["id"],
            "severity": result["signal"]["severity"],
            "title": result["signal"]["title"],
            "description": result["signal"]["description"],
            "context": result.get("context", {}),
            "score": float(result["signal"]["score"]),
            "timestamp": result["signal"]["timestamp"],
            "source": result["signal"]["source"],
        }
    except Exception as exc:
        # Do not leak internals; 503 for downstream failures
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"error": "signal unavailable", "detail": str(exc)},
        )
```

---

### 3) Graph query helper (deterministic, no mutations)

```python
# services/knowledge_rag.py
from datetime import date
from typing import Dict, Any

def get_top_hub_and_signal(as_of: date) -> Dict[str, Any]:
    """
    Deterministic selection:
    1) Highest-degree hub for `as_of` (today).
    2) Strongest cost-anomaly edge from that hub.
    No writes.
    """
    # Example using existing graph client; adapt to your driver
    from graph_client import g  # pseudo import

    # 1) Top hub today (highest degree)
    top_hub = (
        g.V()
        .hasLabel("hub")
        .has("date", str(as_of))
        .order().by(__.bothE().count(), "desc")
        .limit(1)
        .valueMap(True)
        .next()
    )

    hub_id = top_hub.id
    hub_name = top_hub.get("name", "Unknown")
    hub_type = top_hub.get("type", "insight")

    # 2) Strongest cost-anomaly signal edge from this hub
    strongest = (
        g.V(hub_id)
        .outE("cost_anomaly")
        .order().by("score", "desc")
        .limit(1)
        .valueMap(True)
        .next()
    )

    signal_node = g.V(strongest.inV.id).valueMap(True).next()

    return {
        "hub": {"id": hub_id, "name": hub_name, "type": hub_type},
        "signal": {
            "id": signal_node.id,
            "severity": signal_node.get("severity", "medium"),
            "title": signal_node.get("title", "Unnamed anomaly"),
            "description": signal_node.get("description", ""),
            "score": float(signal_node.get("score", 0.0)),
            "timestamp": signal_node.get("timestamp", ""),
            "source": signal_node.get("source", "knowledge-graph"),
        },
        "context": strongest.get("context", {}),
    }
```

---

### 4) Frontend API client (CDN-friendly, graceful)

```ts
// src/lib/api/costAnomaly.ts
import { API_V1 } from './routes';
import type { TopHubCostAnomalySignal } from './types';

export async function fetchTopHubCostAnomalySignal(
  options: { timeoutMs?: number } = {}
): Promise<TopHubCostAnomalySignal | null> {
  const { timeoutMs = 8000 } = options;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(API_V1.COST_ANOMALY_TOP_HUB, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
      cache: 'no-store',
    });

    clearTimeout(timeout);

    if (!res.ok) {
      console.warn('Top-hub signal unavailable', res.status, res.statusText);
      return null;
    }

    return (await res.json()) as TopHubCostAnomalySignal;
  } catch (err) {
    clearTimeout(timeout);
    if ((err as Error).name !== 'AbortError') {
      console.error('Failed to fetch top-hub cost anomaly signal', err);
    }
    return null;
  }
}
```

---

### 5) React hook + component (minimal, non-breaking)

```tsx
// src/hooks/useTopHubSignal.ts
import { useEffect, useState } from 'react';
import { fetchTopHubCostAnomalySignal } from '../lib/api/costAnomaly';
import type { TopHubCostAnomalySignal } from '../lib/api/types';

export function useTopHubSignal(pollIntervalMs = 60000) {
  const [signal, setSignal] = useState<TopHubCostAnomalySignal | null>(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    const result = await fetchTopHubCostAnomalySignal();
    setSignal(result);
    setLoading(false);
  };

  useEffect(() => {
    load();
    const id = setInterval(load, pollIntervalMs);
    return () => clearInterval(id);
  }, [pollIntervalMs]);

  return { signal, loading };
}
```

```tsx
// src/components/TopHubSignalCard.tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';

export function TopHubSignalCard() {
  const { signal, loading } = useTopHubSignal();

  if (loading) return <div className="text-sm text-gray-500">Loading top signal…</div>;
  if (!signal) return null;

  const severityColor = {
    low: 'bg-gray-100 text-gray-800',
    medium: 'bg-yellow-100 text-yellow-800',
    high: 'bg-orange-100 text-orange-800',
    critical: 'bg-red-100 text-red-800',
  }[signal.severity];

  return (
    <div className="rounded border p-3">
      <div className="flex items-center justify-between">
        <span className="font-semib
