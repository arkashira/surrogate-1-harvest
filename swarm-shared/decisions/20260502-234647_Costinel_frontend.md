# Costinel / frontend

## Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Highest-value, read-only frontend card (<2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) and top anomaly signal from backend in a compact, actionable card.

### What I’ll ship
- New component: `TopHubSignalCard`
- Hook: `useTopHubSignal()` to fetch `GET /api/v1/cost-anomaly/signal/top-hub`
- Integrate into dashboard main view (replace placeholder or add to signals row)
- Minimal, accessible UI with trend sparkline, coverage badge, and “View details” link (no execute actions)

### Why this is highest value
- Directly applies “top-hub doc insight” pattern (review most-connected hub)
- Complements existing backend signal endpoint (20260502-234615_Costinel_backend.md)
- Visible impact in <2h with no schema changes or execute flows

---

## Implementation Steps

1. Add types (if not present) for top-hub payload
2. Create `useTopHubSignal` hook (React + fetch, with polling every 60s)
3. Create `TopHubSignalCard` component (card layout, sparkline, badges, link)
4. Wire into dashboard page (main signals row)
5. Basic styling (reuse existing design tokens)

---

## Code Snippets

### 1) Types (add to `src/types/cost.ts` or similar)

```ts
// src/types/cost.ts
export interface TopHubSignal {
  hubId: string;          // e.g. "MOC"
  hubName: string;        // e.g. "Mission Operations Center"
  description?: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  score: number;          // 0-100
  trend: number;          // % change vs prior period
  coverage: number;       // % of resources covered by signal
  lastUpdated: string;    // ISO timestamp
  sparkline: number[];    // recent values (7-14 points)
  signalId?: string;      // linkable signal
}
```

---

### 2) Hook: `useTopHubSignal`

```tsx
// src/hooks/useTopHubSignal.ts
import { useEffect, useState, useCallback } from 'react';
import { TopHubSignal } from '../types/cost';

const POLL_INTERVAL = 60_000; // 60s

export function useTopHubSignal(options?: { enabled?: boolean }) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchSignal = useCallback(async () => {
    if (!options?.enabled) {
      setLoading(false);
      return;
    }
    try {
      const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        credentials: 'same-origin'
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Unknown error'));
    } finally {
      setLoading(false);
    }
  }, [options?.enabled]);

  useEffect(() => {
    fetchSignal();
    const id = setInterval(fetchSignal, POLL_INTERVAL);
    return () => clearInterval(id);
  }, [fetchSignal]);

  return { data, loading, error, refetch: fetchSignal };
}
```

---

### 3) Component: `TopHubSignalCard`

```tsx
// src/components/TopHubSignalCard.tsx
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import { Sparkline } from './Sparkline'; // small reusable sparkline
import { Badge } from './Badge';

function severityColor(sev: TopHubSignal['severity']) {
  switch (sev) {
    case 'critical': return 'text-red-600 bg-red-50 border-red-200';
    case 'high': return 'text-orange-600 bg-orange-50 border-orange-200';
    case 'medium': return 'text-yellow-600 bg-yellow-50 border-yellow-200';
    default: return 'text-blue-600 bg-blue-50 border-blue-200';
  }
}

export function TopHubSignalCard() {
  const { data, loading, error } = useTopHubSignal({ enabled: true });

  if (loading) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50 animate-pulse">
        <div className="h-4 w-24 bg-gray-200 rounded mb-2"></div>
        <div className="h-6 w-32 bg-gray-200 rounded mb-3"></div>
        <div className="h-16 bg-gray-200 rounded"></div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50 text-gray-500">
        Signal unavailable
      </div>
    );
  }

  const badgeCls = severityColor(data.severity);

  return (
    <div className="p-4 border rounded-lg bg-white shadow-sm">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-sm font-medium text-gray-500">Top Hub Signal</h3>
          <p className="text-lg font-semibold text-gray-900">{data.hubName}</p>
          <p className="text-xs text-gray-400">{data.hubId}</p>
        </div>
        <Badge className={badgeCls}>{data.severity.toUpperCase()}</Badge>
      </div>

      <div className="flex items-end justify-between mb-3">
        <div>
          <p className="text-2xl font-bold text-gray-900">{data.score.toFixed(0)}</p>
          <p className="text-xs text-gray-500">Signal score</p>
        </div>
        <div className="text-right">
          <p className={`text-sm font-medium ${data.trend >= 0 ? 'text-red-600' : 'text-green-600'}`}>
            {data.trend >= 0 ? '+' : ''}{data.trend.toFixed(1)}%
          </p>
          <p className="text-xs text-gray-500">trend</p>
        </div>
      </div>

      <div className="h-12 mb-3">
        <Sparkline values={data.sparkline} color={severityColor(data.severity).split(' ')[0]} />
      </div

      <div className="flex items-center justify-between text-xs text-gray-500">
        <span>Coverage {data.coverage.toFixed(0)}%</span>
        <a
          href={`/signals/${data.signalId || ''}`}
          className="text-blue-600 hover:underline font-medium"
        >
          View details →
        </a>
      </div>
    </div>
  );
}
```

---

### 4) Sparkline (minimal)

```tsx
// src/components/Sparkline.tsx
import React from 'react';

interface SparklineProps {
  values: number[];
  color?: string;
}

export function Sparkline({ values, color = '#3B82F6' }: SparklineProps) {
  if (!values?.length) return null;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const points = values.map((v, i) => {
    const x = (i / Math.max(values.length - 1, 1)) *
