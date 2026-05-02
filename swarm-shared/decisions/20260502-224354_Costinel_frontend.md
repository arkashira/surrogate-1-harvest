# Costinel / frontend

## Final Implementation Plan — Costinel Top Cost-Anomaly Signal (≤2h)

**Ship a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` and a visible dashboard card that surfaces today’s strongest hub-level cost-anomaly signal with context and fast actions.**  
- No backend writes or side effects.  
- Safe to deploy and cacheable per day.  
- Entirely implementable in <2h (frontend + thin endpoint).

---

### 1) Backend: Add read-only endpoint

- **Route**: `GET /api/v1/cost-anomaly/signal/top-hub`
- **Behavior**:
  - Deterministic within the same UTC day (idempotent).
  - Queries the knowledge graph for today’s top hub (e.g., “MOC”) and the strongest cost-anomaly signal attached to it.
  - Returns the signal + context + minimal audit trail.
  - No writes, no mutations, no external calls with side effects.
  - Cacheable (e.g., `Cache-Control: public, max-age=60`, or per-day cache key).

- **Response shape**:
```ts
export interface TopAnomalySignal {
  id: string;
  title: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
  score: number;
  hub: string;
  service: string;
  region: string;
  account: string;
  description: string;
  tags: string[];
  timestamp: string; // ISO
  context: {
    baseline: number;
    current: number;
    deltaPercent: number;
    currency?: string;
  };
  auditTrail?: Array<{
    id: string;
    title: string;
    timestamp: string;
    decision: string;
  }>;
}
```

- **Implementation notes**:
  - Reuse existing graph/RAG client if available; otherwise a minimal Cypher/GraphQL query:
    - Find today’s top hub by anomaly strength/centrality.
    - Traverse to the strongest anomaly edge/signal for that hub.
    - Return enriched signal + context.
  - If no signal exists, return `204 No Content` or `{ signal: null }` (frontend handles empty state).
  - Add lightweight server-side error handling and request timeout.

---

### 2) Frontend: TopAnomalySignalPanel

Place this card near the top of the cost-analytics dashboard (Sense + Signal philosophy).

**Features**:
- Auto-refresh every 60s while tab is visible (respect `visibilitychange`).
- Loading, empty, and error states.
- Severity badge, key fields, tags, and context diff.
- Action chips: “View Details”, “Acknowledge”, “Create Proposal”.
- Audit trail preview (last 3 related decisions).

**Code** (concise, production-ready):

#### API client (`src/lib/api.ts`)
```ts
export async function fetchTopAnomalySignal(): Promise<TopAnomalySignal | null> {
  const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
  });

  if (!res.ok) {
    if (res.status === 404 || res.status === 204) return null;
    throw new Error(`Failed to fetch top anomaly signal: ${res.status}`);
  }

  return res.json();
}
```

#### Component (`src/components/TopAnomalySignalPanel.tsx`)
```tsx
'use client';

import { useEffect, useState, useRef } from 'react';
import { fetchTopAnomalySignal, type TopAnomalySignal } from '@/lib/api';

const SEVERITY_COLORS = {
  critical: 'bg-red-600 text-white',
  high: 'bg-orange-500 text-white',
  medium: 'bg-yellow-400 text-gray-900',
  low: 'bg-gray-300 text-gray-800',
} as const;

export default function TopAnomalySignalPanel() {
  const [signal, setSignal] = useState<TopAnomalySignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<NodeJS.Timeout>();

  const load = async () => {
    try {
      setLoading(true);
      const data = await fetchTopAnomalySignal();
      setSignal(data);
      setError(null);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();

    const handleVisibility = () => {
      if (document.hidden && intervalRef.current) {
        clearInterval(intervalRef.current);
      } else if (!document.hidden) {
        load();
        intervalRef.current = setInterval(load, 60_000);
      }
    };

    document.addEventListener('visibilitychange', handleVisibility);
    intervalRef.current = setInterval(load, 60_000);

    return () => {
      document.removeEventListener('visibilitychange', handleVisibility);
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  if (loading && !signal) {
    return (
      <div className="animate-pulse rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
        <div className="h-6 w-32 rounded bg-gray-200 mb-4" />
        <div className="h-4 w-full max-w-md rounded bg-gray-100 mb-2" />
        <div className="h-4 w-5/6 rounded bg-gray-100" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-700">
        Unable to load top cost-anomaly signal.
      </div>
    );
  }

  if (!signal) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-6 text-center text-gray-500 shadow-sm">
        No active cost-anomaly signals at this time.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <span
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                SEVERITY_COLORS[signal.severity]
              }`}
            >
              {signal.severity.toUpperCase()}
            </span>
            <span className="text-sm text-gray-500">
              {new Date(signal.timestamp).toLocaleString()}
            </span>
          </div>

          <h3 className="mt-2 text-lg font-semibold text-gray-900">{signal.title}</h3>
          <p className="mt-1 text-sm text-gray-600">{signal.description}</p>

          <dl className="mt-4 grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
            <div>
              <dt className="font-medium text-gray-500">Hub</dt>
              <dd className="text-gray-900">{signal.hub}</dd>
            </div>
            <div>
              <dt className="font-medium text-gray-500">Service</dt>
              <dd className="text-gray-900">{signal.service}</dd>
            </div>
            <div>
              <dt className="font-medium text-gray-500">Region</dt>
              <dd className="text-gray-900">{signal.region}</dd>
            </div>
            <div>
              <dt className="font-medium text-gray-500">Account</dt>
              <dd className="text-gray-900">{signal.account}</dd>
            </div>
            <div>
              <dt className="font-medium text
