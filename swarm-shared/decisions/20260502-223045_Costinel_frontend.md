# Costinel / frontend

## Highest-Value Incremental Improvement (<2h)

**Improvement:** Add a deterministic, read-only **Cost Anomaly Signal** widget to the frontend dashboard that surfaces the single strongest service-level anomaly for today against a 7-day trailing baseline — no execution, no mutations, no side effects.

**Why this ships fast:**
- Reuses existing `/api/v1/cost-anomaly/signal` backend endpoint (confirmed in swarm decisions).
- No new backend work; only a frontend component + lightweight data fetch.
- Fits Costinel philosophy: *Sense + Signal — ไม่ Execute*.
- Provides immediate user value (visibility) without governance/change-management complexity.

---

## Implementation Plan

1. **Create `CostAnomalySignalWidget` component**
   - Location: `src/components/CostAnomalySignalWidget.tsx`
   - Fetches `GET /api/v1/cost-anomaly/signal`
   - Shows: service, delta %, severity, time window, and a sparkline of 7-day baseline vs today.
   - Skeleton loader + empty/error states.

2. **Add to Dashboard layout**
   - Insert into `src/pages/Dashboard.tsx` (or equivalent) near top of cost analytics section.
   - Mobile-first card layout consistent with existing design tokens.

3. **Types & API utilities**
   - Add `CostAnomalySignal` type in `src/types/cost.ts`.
   - Add `fetchCostAnomalySignal()` in `src/lib/api/cost.ts` using existing API client pattern.

4. **Polling (optional but recommended)**
   - Refresh every 5–10 minutes while tab is visible (respects battery/network).

5. **Tests & lint**
   - Basic smoke test for component render and fetch integration.

---

## Code Snippets

### 1. Type definition

```ts
// src/types/cost.ts
export interface CostAnomalySignal {
  service: string;            // e.g., "AmazonEC2"
  accountId: string;          // cloud account identifier
  region: string;             // e.g., "us-east-1"
  todayAmount: number;        // today's spend for this service
  baselineAmount: number;     // 7-day trailing average
  deltaPercent: number;       // (today - baseline) / baseline * 100
  severity: 'low' | 'medium' | 'high' | 'critical';
  currency: string;           // e.g., "USD"
  timestamp: string;          // ISO 8601 (UTC)
}
```

---

### 2. API utility

```ts
// src/lib/api/cost.ts
import { CostAnomalySignal } from '../types/cost';

const API_BASE = '/api/v1';

export async function fetchCostAnomalySignal(): Promise<CostAnomalySignal | null> {
  try {
    const res = await fetch(`${API_BASE}/cost-anomaly/signal`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'same-origin',
    });

    if (!res.ok) {
      // Non-fatal for read-only signal; degrade gracefully
      console.warn('Cost anomaly signal unavailable:', res.status);
      return null;
    }

    return (await res.json()) as CostAnomalySignal;
  } catch (err) {
    console.error('Failed to fetch cost anomaly signal:', err);
    return null;
  }
}
```

---

### 3. Widget component

```tsx
// src/components/CostAnomalySignalWidget.tsx
import { useEffect, useState, useCallback } from 'react';
import { fetchCostAnomalySignal } from '../lib/api/cost';
import { CostAnomalySignal } from '../types/cost';

function classNames(...classes: string[]) {
  return classes.filter(Boolean).join(' ');
}

function severityColor(severity: CostAnomalySignal['severity']) {
  switch (severity) {
    case 'critical':
      return 'bg-red-500/10 text-red-500 ring-red-500/20';
    case 'high':
      return 'bg-orange-500/10 text-orange-500 ring-orange-500/20';
    case 'medium':
      return 'bg-yellow-500/10 text-yellow-500 ring-yellow-500/20';
    case 'low':
      return 'bg-blue-500/10 text-blue-500 ring-blue-500/20';
    default:
      return 'bg-gray-500/10 text-gray-500 ring-gray-500/20';
  }
}

export function CostAnomalySignalWidget() {
  const [signal, setSignal] = useState<CostAnomalySignal | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const data = await fetchCostAnomalySignal();
    setSignal(data);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(() => {
      // Refresh every 7 minutes while mounted
      load();
    }, 7 * 60 * 1000);

    return () => clearInterval(interval);
  }, [load]);

  if (loading) {
    return (
      <div className="rounded-xl border border-white/5 bg-white/5 p-5 animate-pulse">
        <div className="h-5 w-32 bg-white/10 rounded mb-3"></div>
        <div className="h-8 w-40 bg-white/10 rounded mb-2"></div>
        <div className="h-4 w-24 bg-white/10 rounded"></div>
      </div>
    );
  }

  if (!signal) {
    return null; // Graceful no-op when signal unavailable
  }

  const isUp = signal.deltaPercent >= 0;
  const percentAbs = Math.abs(signal.deltaPercent).toFixed(1);

  return (
    <div className="rounded-xl border border-white/5 bg-white/5 p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm font-medium text-white/60">Anomaly Signal</p>
          <p className="mt-1 text-xl font-semibold text-white">
            {signal.service}
          </p>
          <p className="text-sm text-white/40">
            {signal.accountId} / {signal.region}
          </p>
        </div>
        <span
          className={classNames(
            'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset',
            severityColor(signal.severity)
          )}
        >
          {signal.severity.toUpperCase()}
        </span>
      </div>

      <div className="mt-4 flex items-end justify-between">
        <div>
          <p className="text-2xl font-bold text-white">
            {signal.todayAmount.toLocaleString(undefined, {
              style: 'currency',
              currency: signal.currency,
              minimumFractionDigits: 2,
            })}
          </p>
          <p className="text-sm text-white/40">Today</p>
        </div>

        <div className="text-right">
          <p
            className={classNames(
              'text-lg font-semibold',
              isUp ? 'text-red-400' : 'text-green-400'
            )}
          >
            {isUp ? '+' : '-'}{percentAbs}%
          </p>
          <p className="text-sm text-white/40">vs 7-day avg</p>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2 text-xs text-white/40">
        <span>Signal generated</span>
        <span className="text-white/60">
          {new Date(signal.timestamp).toLocaleString(undefined, {
            dateStyle: 'short',
            timeStyle: 'short',
            timeZone: 'UTC',
          })}{' '}
          UTC
        </span>
      </div>
    </div>
  );
}
```

---

### 4.
