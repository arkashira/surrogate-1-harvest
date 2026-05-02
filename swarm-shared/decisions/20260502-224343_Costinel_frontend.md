# Costinel / frontend

## Final Synthesis — One Correct, Actionable Plan (≤2h)

**Chosen highest-value improvement**  
Add a **read-only, deterministic** `GET /api/v1/cost-anomaly/signal/top-hub` endpoint plus a frontend panel that renders today’s top hub and its strongest cost-anomaly signal with a knowledge-graph drill-down link.  
- No writes, no schema changes, no side effects.  
- Pure visibility (“Sense + Signal — لا Execute”).  
- Reuses existing knowledge-graph/RAG tooling.

---

### Why this wins
- Combines Candidate 2’s backend necessity with Candidate 1’s frontend completeness.  
- Correctness: endpoint must exist before UI is useful; both layers are required for immediate value.  
- Actionability: concrete, minimal code, clear verification, and a <2h checklist.

---

### Concrete implementation (backend + frontend)

#### 1) Backend: FastAPI route (≈15–30m)

`src/main.py` (or your routes module)
```python
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from typing import Optional

from .knowledge_rag import identify_top_hub_today, extract_strongest_cost_anomaly_signal

router = APIRouter()

class TopHubSignalResponse(BaseModel):
    hubId: str
    hubLabel: str
    signalId: str
    severity: str  # low|medium|high|critical
    title: str
    description: str
    context: dict
    timestamp: str  # ISO
    graphUrl: Optional[str] = None

@router.get("/api/v1/cost-anomaly/signal/top-hub", response_model=TopHubSignalResponse)
async def get_top_hub_signal():
    try:
        # 1) Identify top hub for today (reuse existing RAG/graph tooling)
        top_hub = identify_top_hub_today(as_of=datetime.now(timezone.utc).date())
        if not top_hub:
            raise HTTPException(status_code=404, detail="No top hub found for today")

        # 2) Extract strongest cost-anomaly signal for this hub
        signal = extract_strongest_cost_anomaly_signal(hub=top_hub)
        if not signal:
            raise HTTPException(status_code=404, detail="No cost-anomaly signal found")

        # 3) Build response (normalize fields expected by frontend)
        return TopHubSignalResponse(
            hubId=top_hub.id,
            hubLabel=top_hub.label or top_hub.id,
            signalId=signal.id,
            severity=signal.severity,
            title=signal.title,
            description=signal.description,
            context=signal.context or {},
            timestamp=signal.timestamp.isoformat(),
            graphUrl=signal.graph_url,
        )
    except Exception as exc:
        # Log internally; return graceful client error
        raise HTTPException(status_code=500, detail="Unable to fetch top-hub signal") from exc
```

Notes
- `identify_top_hub_today` and `extract_strongest_cost_anomaly_signal` must already exist per ops/quality decisions; if not, stub minimal versions for today (e.g., query graph for hub with highest anomaly score today).  
- No writes, no schema changes.  
- Keep deterministic: same request within a short window → same result.

---

#### 2) Frontend: API helper + panel (≈30–45m)

`src/lib/api.ts`
```ts
export interface TopHubSignal {
  hubId: string;
  hubLabel: string;
  signalId: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  description: string;
  context: Record<string, unknown>;
  timestamp: string; // ISO
  graphUrl?: string;
}

export async function fetchTopHubSignal(): Promise<TopHubSignal | null> {
  try {
    const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });

    if (!res.ok) {
      // graceful degradation
      console.warn('Top-hub signal unavailable', res.status);
      return null;
    }

    return (await res.json()) as TopHubSignal;
  } catch (err) {
    console.error('Failed to fetch top-hub signal', err);
    return null;
  }
}
```

`src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal, TopHubSignal } from '../lib/api';

const severityColors = {
  low: 'bg-blue-100 text-blue-800 border-blue-200',
  medium: 'bg-yellow-100 text-yellow-800 border-yellow-200',
  high: 'bg-orange-100 text-orange-800 border-orange-200',
  critical: 'bg-red-100 text-red-800 border-red-200',
} as const;

export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubSignal()
      .then((data) => {
        if (mounted) setSignal(data);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-2 h-4 w-24 animate-pulse rounded bg-gray-100" />
      </div>
    );
  }

  if (!signal) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 text-sm text-gray-500 shadow-sm">
        No top-hub signal available at this time.
      </div>
    );
  }

  return (
    <div className={`rounded-lg border ${severityColors[signal.severity]} p-4 shadow-sm`}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-semibold">Top Hub</span>
            <span className="rounded bg-white/60 px-2 py-0.5 text-xs font-mono font-medium text-gray-700">
              {signal.hubId}
            </span>
          </div>
          <p className="mt-1 text-sm font-medium text-gray-800">{signal.title}</p>
          <p className="mt-1 text-sm text-gray-600">{signal.description}</p>

          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            {signal.graphUrl && (
              <a
                href={signal.graphUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-2 hover:no-underline"
              >
                Open in Knowledge Graph
              </a>
            )}
            <span className="text-gray-400">{new Date(signal.timestamp).toLocaleString()}</span>
          </div>
        </div>

        <div className="shrink-0">
          <span
            className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase ${
              {
                low: 'bg-blue-200 text-blue-900',
                medium: 'bg-yellow-200 text-yellow-900',
                high: 'bg-orange-200 text-orange-900',
                critical: 'bg-red-200 text-red-900',
              }[signal.severity]
            }`}
          >
            {signal.severity}
          </span>
        </div>
      </div>
    </div>
  );

