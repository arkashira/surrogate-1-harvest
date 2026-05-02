# Costinel / frontend

## Final Answer — Synthesized, Correct, Actionable (≤2h)

**Goal:** Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint and minimal UI surface that shows today’s strongest cost‑anomaly signal for the top hub (e.g., MOC). No writes, no side effects. Fits Costinel “Sense + Signal” philosophy.

---

## 1) API contract (single source of truth)

`types/cost-anomaly.ts`
```ts
export interface CostAnomalySignal {
  hub: string;               // e.g. "MOC"
  hubLabel?: string;         // human readable
  signalId: string;
  signalType?: 'cost-anomaly';
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  description: string;
  context: string;
  entities: string[];        // resource IDs / ARNs
  timestamp: string;         // ISO date (YYYY-MM-DD)
}
```

---

## 2) Backend — API route (Next.js)

`pages/api/v1/cost-anomaly/signal/top-hub.ts`
```ts
import type { NextApiRequest, NextApiResponse } from 'next';
import { getTopHubSignalForDate } from '@/lib/knowledge-rag/graph';
import type { CostAnomalySignal } from '@/types/cost-anomaly';

// Lightweight in-memory cache to avoid repeated heavy queries during dev/refresh storms.
let cached: { data: CostAnomalySignal | null; ts: number } | null = null;
const CACHE_TTL_MS = 60_000; // 60s

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const now = Date.now();
  if (cached && now - cached.ts < CACHE_TTL_MS) {
    return cached.data ? res.status(200).json(cached.data) : res.status(204).end();
  }

  try {
    const today = new Date().toISOString().slice(0, 10); // UTC YYYY-MM-DD
    const signal = await getTopHubSignalForDate(today);

    cached = { data: signal, ts: now };

    if (!signal) {
      return res.status(204).end();
    }
    return res.status(200).json(signal);
  } catch (err) {
    console.error('Failed to fetch top-hub anomaly signal', err);
    return res.status(500).json({ error: 'Failed to fetch signal' });
  }
}
```

---

## 3) Knowledge‑RAG/graph adapter (stub for integration)

`lib/knowledge-rag/graph.ts`
```ts
import type { CostAnomalySignal } from '@/types/cost-anomaly';

// TODO: Replace mock with real graph query using existing knowledge-rag utilities.
// Example: reuse graph client to find top hub and strongest cost-anomaly signal for date.
export async function getTopHubSignalForDate(date: string): Promise<CostAnomalySignal | null> {
  // Placeholder: integrate with existing graph query client.
  // const topHub = await graph.topHub({ category: 'cost-anomaly', date });
  // const signal = await graph.strongestSignal({ hub: topHub.name, date });

  if (process.env.NODE_ENV === 'development') {
    return {
      hub: 'MOC',
      hubLabel: 'Mission Operations Center',
      signalId: 'dev-signal-001',
      signalType: 'cost-anomaly',
      severity: 'high',
      title: 'Unexpected EC2 spend spike in us-east-1',
      description: 'Detected 3.2x baseline spend on m5.large instances; likely orphaned staging fleet.',
      context: 'Instances launched outside tagged cost-center; no auto-scaling policy.',
      entities: ['i-0abc123', 'i-0def456'],
      timestamp: date,
    };
  }

  return null;
}
```

---

## 4) Frontend hook (SWR)

`hooks/useTopHubAnomalySignal.ts`
```ts
import useSWR from 'swr';
import type { CostAnomalySignal } from '@/types/cost-anomaly';

const fetcher = (url: string) =>
  fetch(url).then((r) => {
    if (r.status === 204) return null;
    if (!r.ok) throw new Error('Failed to fetch');
    return r.json() as Promise<CostAnomalySignal>;
  });

export function useTopHubAnomalySignal() {
  const { data, error, isValidating } = useSWR<CostAnomalySignal | null>(
    '/api/v1/cost-anomaly/signal/top-hub',
    fetcher,
    { refreshInterval: 60_000, revalidateOnFocus: false }
  );

  return {
    signal: data,
    isLoading: !error && !data && isValidating,
    error,
  };
}
```

---

## 5) UI surface — compact, non‑disruptive banner

`components/CostAnomalyTopHubBanner.tsx`
```tsx
import { useTopHubAnomalySignal } from '@/hooks/useTopHubAnomalySignal';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';

const severityColors = {
  low: 'bg-blue-50 text-blue-800 border-blue-200',
  medium: 'bg-yellow-50 text-yellow-800 border-yellow-200',
  high: 'bg-orange-50 text-orange-800 border-orange-200',
  critical: 'bg-red-50 text-red-800 border-red-200',
} as const;

export function CostAnomalyTopHubBanner() {
  const { signal, isLoading } = useTopHubAnomalySignal();

  if (isLoading || !signal) return null;

  return (
    <div className={`rounded border p-3 ${severityColors[signal.severity]}`}>
      <div className="flex items-start gap-2">
        <ExclamationTriangleIcon className="mt-0.5 h-5 w-5 flex-none" />
        <div className="flex-1">
          <p className="font-medium">{signal.title}</p>
          <p className="text-sm opacity-90">{signal.description}</p>
          {signal.context && (
            <p className="mt-1 text-xs opacity-80">{signal.context}</p>
          )}
        </div>
      </div>
    </div>
  );
}
```

Add `<CostAnomalyTopHubBanner />` near the top of the dashboard layout.

---

## 6) Implementation checklist (≤2h)

| Step | Owner | Time |
|------|-------|------|
| Add types (`CostAnomalySignal`) | FE | 10m |
| Create API route + caching | BE | 30m |
| Wire `getTopHubSignalForDate` to existing knowledge‑RAG/graph utilities | BE | 20m |
| Create hook (`useTopHubAnomalySignal`) | FE | 10m |
| Create banner component + integrate into dashboard | FE | 20m |
| Smoke test (deterministic, no writes, 200/204/500) | QA/Dev | 10m |

**Total:** ~90 minutes.

---

## 7) Notes & follow-ups

- Replace the mock in `getTopHubSignalForDate` with real graph queries using existing knowledge‑RAG utilities.
- Keep the endpoint read-only and side-effect free.
- Cache TTL can be increased later if query cost is acceptable.
- If stricter SLA is required, add request timeout and circuit-breaker around graph calls.
