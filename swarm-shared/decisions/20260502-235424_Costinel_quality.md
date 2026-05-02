# Costinel / quality

## Final Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, rationale, related docs, and audit trail — delivered via a real backend endpoint with a resilient, mock-friendly frontend.

---

### 1) Highest-value incremental improvement
Add a **Top-Hub Signal Card** to the dashboard that:
- Queries the knowledge-rag graph (via backend) for the most-connected hub (by degree/centrality)
- Shows hub name, score, short rationale, and top 3 related docs
- Links to related docs (opens externally or in new tab)
- Emits a read-only audit log entry for governance
- Uses a real backend endpoint for production, while keeping the frontend service layer mockable for fast iteration

This satisfies “Sense + Signal” with concrete auditability and zero mutations.

---

### 2) Implementation steps (≤2h)

1. **Backend** (20 min)
   - Add read-only endpoint `GET /api/signal/top-hub`
   - Call knowledge-rag to get top hub + related docs
   - Return `{ hub, score, rationale, relatedDocs, ts }`
   - Emit read-only audit log entry

2. **Frontend service layer** (15 min)
   - Create `src/services/topHub.ts`
   - Expose `fetchTopHub(): Promise<Hub>`
   - Default to typed mock (MOC) for dev/demo; swap to real tRPC call in integration

3. **Frontend card** (45 min)
   - Create `TopHubSignalCard` component
   - Use service layer (mock or real) to fetch data
   - Render hub name, score, rationale, related docs list, timestamp
   - Style consistent with existing dashboard cards; accessible and responsive

4. **Routing + integration** (15 min)
   - Add card to dashboard layout (e.g., in the “Signals” row)
   - Ensure no write/execute controls present

5. **Polish + tests** (25 min)
   - Add loading/error states
   - Verify audit log entry is read-only
   - Smoke test in dev with both mock and real endpoint

---

### 3) Code snippets

#### Backend: `GET /api/signal/top-hub`
```ts
// src/server/api/routers/signal.ts
import { createTRPCRouter, publicProcedure } from '../trpc';
import { auditLog } from '~/server/audit';

export const signalRouter = createTRPCRouter({
  topHub: publicProcedure.query(async () => {
    // 1) Query knowledge-rag for most-connected hub
    const topHub = await knowledgeRag.getMostConnectedHub(); // { hub: 'MOC', score: 0.94, rationale: '...' }

    // 2) Get top 3 related docs
    const relatedDocs = await knowledgeRag.getRelatedDocs(topHub.hub, { limit: 3 });

    const result = {
      hub: topHub.hub,
      score: topHub.score,
      rationale: topHub.rationale,
      relatedDocs,
      ts: new Date().toISOString(),
    };

    // 3) Emit read-only signal audit entry
    auditLog.emit({
      action: 'signal.top_hub.read',
      actor: 'system',
      target: result.hub,
      metadata: { score: result.score, relatedCount: result.relatedDocs.length },
      severity: 'low',
    });

    return result;
  }),
});
```

#### Frontend service layer (mockable)
```ts
// src/services/topHub.ts
import { api } from '~/utils/api';

export interface RelatedDoc {
  id?: string;
  title: string;
  url: string;
  snippet?: string;
}

export interface Hub {
  name: string;
  score: number;
  rationale: string;
  relatedDocs: RelatedDoc[];
  ts?: string;
}

// Deterministic mock for fast dev/demo
async function getMockTopHub(): Promise<Hub> {
  await new Promise((r) => setTimeout(r, 120));
  return {
    name: 'MOC',
    score: 0.92,
    rationale:
      'Most-connected hub across cost governance policies and anomaly patterns; central to cross-cloud tagging and ownership signals.',
    relatedDocs: [
      {
        title: 'Tagging Strategy & Ownership Model',
        url: '/docs/tagging-strategy',
        snippet: 'Standardized tags for cost-center, owner, and environment to improve allocation accuracy.',
      },
      {
        title: 'Anomaly Detection Patterns',
        url: '/docs/anomalies',
        snippet: 'Common spike and idle patterns observed across linked accounts.',
      },
      {
        title: 'Cost Allocation Best Practices',
        url: '/docs/cost-allocation',
        snippet: 'Guidance for mapping spend to business units with minimal leakage.',
      },
    ],
  };
}

// Use mock in dev/demo, real endpoint in prod-like environments
export async function fetchTopHub(useMock = process.env.NODE_ENV !== 'production'): Promise<Hub> {
  if (useMock) return getMockTopHub();

  const data = await api.signal.topHub.query();
  return {
    name: data.hub,
    score: data.score,
    rationale: data.rationale,
    relatedDocs: data.relatedDocs,
    ts: data.ts,
  };
}
```

#### Frontend: `TopHubSignalCard`
```tsx
// src/components/TopHubSignalCard.tsx
import { useEffect, useState } from 'react';
import { fetchTopHub, type Hub } from '~/services/topHub';
import { Clock, FileText, TrendingUp } from 'lucide-react';

export function TopHubSignalCard() {
  const [hub, setHub] = useState<Hub | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub()
      .then((data) => {
        if (mounted) setHub(data);
      })
      .catch((err) => {
        if (mounted) setError(err instanceof Error ? err.message : 'Unable to load signal.');
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) return <div className="card">Loading signal…</div>;
  if (error) return <div className="card text-red-500">{error}</div>;
  if (!hub) return null;

  return (
    <div className="card" role="region" aria-label="Top hub signal">
      <div className="flex items-center gap-2 mb-3">
        <TrendingUp className="w-5 h-5 text-blue-500" aria-hidden="true" />
        <h3 className="font-semibold">Top Hub Signal</h3>
      </div>

      <div className="mb-2">
        <span className="text-2xl font-bold">{hub.name}</span>
        <span className="ml-2 text-sm text-gray-500">({Math.round(hub.score * 100)}%)</span>
      </div>

      <p className="text-sm text-gray-600 mb-3">{hub.rationale}</p>

      <div className="space-y-1 mb-3" aria-label="Related documents">
        {hub.relatedDocs.map((doc) => (
          <a
            key={doc.id ?? doc.url}
            href={doc.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-sm text-blue-600 hover:underline"
          >
            <FileText className="w-3 h-3" aria-hidden="true" />
            <span>{doc.title}</span>
          </a>
        ))}
      </div>

      {hub.ts && (
        <div className="flex items-center gap-1 text-xs text-gray-400
