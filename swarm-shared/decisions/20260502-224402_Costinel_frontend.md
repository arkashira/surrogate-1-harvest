# Costinel / frontend

Candidate 3:
## Highest-value incremental improvement (≤2h)

**Goal**: Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context.  
**Why**: Directly implements the last two swarm decisions, reuses existing RAG/graph infra, and gives the frontend an immediate signal to surface on the dashboard without new writes or side effects.

---

## Implementation plan (frontend-focused)

1. **Backend (minimal, read-only)**
   - Add `GET /api/v1/cost-anomaly/signal/top-hub`
   - Query knowledge graph for today’s top hub (e.g., “MOC” or highest-degree cost node)
   - Return strongest cost-anomaly signal with:
     - `hubId`, `hubLabel`, `hubType`
     - `signalId`, `severity`, `score`
     - `entity` (account/region/service), `windowStart`, `windowEnd`
     - `context` (short list of related nodes/edges)
   - No writes; idempotent; cacheable (5–60s)

2. **Frontend**
   - Add `useTopHubSignal` hook (SWR/React Query)
   - Add dashboard widget “Top cost anomaly” that:
     - Polls endpoint every 30s (or uses SWR revalidateOnFocus)
     - Shows severity badge, entity, and one-line context
     - Links to full hub view (if available)
   - Keep UI read-only; no mutations

3. **Types**
   - Add `TopHubSignalResponse` and related types

4. **Tests**
   - Frontend: snapshot + behavior test for widget
   - Backend: unit test for endpoint (mock graph query)

Estimated: backend ~40m, frontend ~60m, buffer ~20m.

---

## Code snippets

### Frontend: types

```ts
// src/types/costAnomaly.ts
export interface TopHubSignalResponse {
  hubId: string;
  hubLabel: string;
  hubType: 'account' | 'region' | 'service' | 'cost-center';
  signalId: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  score: number;
  entity: {
    id: string;
    name: string;
    type: 'account' | 'region' | 'service';
  };
  windowStart: string; // ISO
  windowEnd: string;   // ISO
  context: Array<{
    nodeId: string;
    label: string;
    relation: string;
  }>;
}
```

### Frontend: hook

```ts
// src/hooks/useTopHubSignal.ts
import useSWR from 'swr';
import { TopHubSignalResponse } from '../types/costAnomaly';

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export function useTopHubSignal(refreshInterval = 30_000) {
  return useSWR<TopHubSignalResponse>(
    '/api/v1/cost-anomaly/signal/top-hub',
    fetcher,
    {
      refreshInterval,
      revalidateOnFocus: true,
      dedupingInterval: 15_000,
    }
  );
}
```

### Frontend: dashboard widget

```tsx
// src/components/dashboard/TopHubSignalWidget.tsx
import { useTopHubSignal } from '../../hooks/useTopHubSignal';
import { Clock, AlertCircle } from 'lucide-react';

const severityColors = {
  critical: 'bg-red-600 text-white',
  high: 'bg-orange-500 text-white',
  medium: 'bg-yellow-400 text-gray-900',
  low: 'bg-gray-300 text-gray-800',
} as const;

export default function TopHubSignalWidget() {
  const { data, error, isLoading } = useTopHubSignal();

  if (isLoading) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50 animate-pulse">
        Loading top signal...
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50 text-gray-500">
        Unable to load top signal.
      </div>
    );
  }

  return (
    <div className="p-4 border rounded-lg bg-white shadow-sm">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <AlertCircle className="w-5 h-5 text-gray-400" />
          <span className="font-semibold text-gray-900">Top cost anomaly</span>
          <span className={`px-2 py-0.5 text-xs font-medium rounded-full ${severityColors[data.severity]}`}>
            {data.severity}
          </span>
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-500">
          <Clock className="w-3 h-3" />
          {new Date(data.windowEnd).toLocaleTimeString()}
        </div>
      </div>

      <div className="mt-3">
        <p className="text-sm text-gray-600">
          <strong className="text-gray-900">{data.entity.name}</strong> in {data.entity.type}
        </p>
        <p className="text-xs text-gray-500 mt-1">
          Hub: {data.hubLabel} ({data.hubType})
        </p>
        {data.context.length > 0 && (
          <p className="text-xs text-gray-400 mt-2">
            Related: {data.context.slice(0, 2).map((c) => c.label).join(', ')}
          </p>
        )}
      </div>
    </div>
  );
}
```

### Backend: endpoint sketch (Node/Express)

```ts
// src/server/routes/costAnomaly.ts
import { Router } from 'express';
import { getTopHubSignal } from '../../services/knowledgeGraph';

const router = Router();

/**
 * GET /api/v1/cost-anomaly/signal/top-hub
 * Returns strongest cost-anomaly signal for today's top hub (read-only).
 */
router.get('/signal/top-hub', async (req, res) => {
  try {
    // Optional: allow ?date=YYYY-MM-DD for testing; default today
    const date = req.query.date || new Date().toISOString().slice(0, 10);
    const signal = await getTopHubSignal(date as string);

    if (!signal) {
      return res.status(404).json({ message: 'No signal found for today' });
    }

    // Cache 30s to reduce graph queries during dashboard refresh
    res.set('Cache-Control', 'public, max-age=30');
    return res.json(signal);
  } catch (err) {
    console.error('Failed to fetch top hub signal', err);
    return res.status(500).json({ message: 'Internal server error' });
  }
});

export default router;
```

### Backend: service stub (graph query)

```ts
// src/services/knowledgeGraph.ts
import type { TopHubSignalResponse } from '../types/costAnomaly';

// Replace with real graph query (e.g., Neo4j/NetworkX via internal API).
// This is a minimal deterministic stub for immediate use.
export async function getTopHubSignal(date: string): Promise<TopHubSignalResponse | null> {
  // Example: query top hub by degree for date, then strongest anomaly edge.
  // In production, call internal RAG/graph service (read-only).
  // For now, return deterministic shape so frontend can render immediately.
  return {
    hubId: 'moc-2026-05-02',
    hubLabel: 'MOC',
    hubType: 'cost-center',
    signalId: `anomaly-${date}-001`,
    severity: 'high',
    score: 0.87,
    entity: {
      id: 'acct-12345678901
