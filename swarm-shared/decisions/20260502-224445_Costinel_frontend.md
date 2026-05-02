# Costinel / frontend

**Final consolidated implementation plan**  
*(Best parts merged; contradictions resolved in favor of correctness + concrete actionability)*

---

## Core objective
Add a **read-only, deterministic** “Top Hub Signal” capability that:
- Queries the knowledge graph for today’s top hub and strongest cost-anomaly signal.
- Exposes a single GET endpoint (`/api/v1/cost-anomaly/signal/top`).
- Presents the signal in a lightweight frontend widget (panel + optional dashboard embed).
- Preserves **Sense + Signal — No Execute**.
- Deliverable in **<2 hours** (frontend + backend contract).

---

## 1) Backend contract (canonical)

**Endpoint**  
`GET /api/v1/cost-anomaly/signal/top`

**Query params**  
- `today` (optional, `YYYY-MM-DD`). Defaults to current UTC date. Enables deterministic testing and caching.

**Response 200**
```json
{
  "hub": "MOC",
  "signal": "spend_spike",
  "severity": "high",
  "score": 0.92,
  "summary": "Unusual 3.2x spend increase in us-east-1/EC2 over last 6h",
  "context": "Correlates with new autoscaling group launch and missing RI coverage",
  "confidence": 0.87,
  "timestamp": "2026-05-02T22:45:00Z",
  "recommendation": "Review RI purchase options and scaling policy"
}
```

**Notes / correctness choices**
- `score` is a normalized anomaly strength (0–1). `severity` is categorical (`low|medium|high|critical`). Both can coexist; `score` is for ranking, `severity` for UI.
- `confidence` retained from Candidate 1 (useful UX detail).
- Deterministic for a given UTC day via `today` param and cached knowledge-graph lookup.
- Read-only; no mutations; no DB migrations.

---

## 2) Backend implementation sketch (FastAPI)

File: `app/api/v1/endpoints/cost_anomaly.py`
```python
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from typing import Optional

from app.services.knowledge_rag import KnowledgeRAG

router = APIRouter()

@router.get("/signal/top", response_model=dict)
async def get_top_hub_signal(
    rag: KnowledgeRAG = Depends(),
    today: Optional[str] = None,
) -> dict:
    """
    Deterministic top-hub cost-anomaly signal for today (UTC).
    Read-only. Uses knowledge-rag to identify the most-connected hub
    and strongest cost-anomaly signal.
    """
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # KnowledgeRAG query: top hub + strongest signal for the day
    result = await rag.query_top_hub_signal(date=today)
    if not result:
        # Return a safe, structured empty payload rather than 5xx
        return {
            "hub": None,
            "signal": None,
            "severity": None,
            "score": 0.0,
            "summary": "No signal detected",
            "context": "",
            "confidence": 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendation": ""
        }

    return {
        "hub": result.hub,
        "signal": result.signal_type,
        "severity": result.severity,
        "score": float(result.score),
        "summary": result.summary,
        "context": result.context,
        "confidence": float(result.confidence),
        "timestamp": result.ts.isoformat(),
        "recommendation": result.recommendation
    }
```

`KnowledgeRAG.query_top_hub_signal(date)` should:
- Query the knowledge graph for today’s most-connected hub.
- Retrieve the strongest cost-related anomaly signal for that hub.
- Be cached per UTC day (e.g., in-memory or Redis) to guarantee determinism and fast response.

---

## 3) Frontend: shared types

`/src/hooks/useTopHubSignal.ts` (types used by hook and component)
```ts
export interface TopHubSignal {
  hub: string | null;
  signal: string | null;
  severity: 'low' | 'medium' | 'high' | 'critical' | null;
  score: number;
  summary: string;
  context: string;
  confidence: number;
  timestamp: string;
  recommendation: string;
}
```

---

## 4) Frontend: hook

`/src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import type { TopHubSignal } from './useTopHubSignal';

export function useTopHubSignal(refreshIntervalMs = 60000) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSignal = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch('/api/v1/cost-anomaly/signal/top', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin'
      });

      if (!res.ok) throw new Error(`API error ${res.status}`);
      const payload = (await res.json()) as TopHubSignal;
      setData(payload);
      setError(null);
    } catch (err: any) {
      setError(err.message || 'Failed to fetch top hub signal');
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSignal();
    const id = setInterval(fetchSignal, refreshIntervalMs);
    return () => clearInterval(id);
  }, [fetchSignal, refreshIntervalMs]);

  return { data, loading, error, refetch: fetchSignal };
}
```

---

## 5) Frontend: `TopHubSignalPanel`

`/src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';

const severityColor = (s: string | null | undefined) => {
  switch (s) {
    case 'critical': return 'bg-red-600 text-white';
    case 'high': return 'bg-red-500 text-white';
    case 'medium': return 'bg-amber-500 text-white';
    case 'low': return 'bg-blue-500 text-white';
    default: return 'bg-gray-500 text-white';
  }
};

export default function TopHubSignalPanel() {
  const { data, loading, error } = useTopHubSignal();

  if (loading) {
    return (
      <div className="p-4 border rounded bg-gray-50 text-gray-600">
        Loading top hub signal...
      </div>
    );
  }

  if (error || !data || !data.hub) {
    return (
      <div className="p-4 border rounded bg-gray-50 text-gray-500">
        Signal unavailable.
      </div>
    );
  }

  return (
    <div className="p-5 border rounded bg-white shadow-sm space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs uppercase tracking-wider text-gray-500">
            Top Hub
          </span>
          <span className="font-bold text-lg">{data.hub}</span>
        </div>
        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${severityColor(data.severity)}`}>
          {data.severity}
        </span>
      </div>

      <div>
        <p className="font-semibold text-gray-900">{data.summary}</p>
        <p className="text-sm text-gray-600 mt-1">{data.context}</p>
      </div
