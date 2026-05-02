# Costinel / frontend

## Final Implementation Plan — Costinel Frontend (Top-Hub Signal UI)

**Scope**: Highest-value incremental frontend feature (<2h)  
**Goal**: Expose `GET /api/v1/cost-anomaly/signal/top-hub` as a visible, read-only “Top-Hub Signal” card in the dashboard so users can **sense and triage** cost anomalies without execution.

**Core principle**: Sense + Signal — **ไม่ Execute** (no mutation/apply buttons).  
**Data source**: Reuse existing backend endpoint; frontend work is pure integration + UI polish.

---

### 1) What to build

- **Dashboard widget**: `TopHubSignalCard`
  - Prominent placement (top row) with responsive behavior.
  - Shows: hub name, severity badge, affected accounts/services, time window, and top 3 anomalies (service + region + delta).
  - Click → slide-out detail drawer with full anomaly list and context (copyable JSON for audit).
  - **Acknowledge** action (persists local/dashboard-level dismissal or timestamp; does not mutate backend).
  - Auto-refresh every 60s (configurable) with SWR/staleness UX.
  - Empty / loading / error states and full keyboard + ARIA accessibility.

---

### 2) Implementation steps (concrete)

1. **Add API client helper**  
   `src/lib/api/signals.ts`  
   - GET `/api/v1/cost-anomaly/signal/top-hub`
   - Exponential backoff on 429/5xx; cancel on unmount.

2. **Create reusable fetch hook**  
   `src/hooks/useTopHubSignal.ts`  
   - Uses SWR or TanStack Query (pick existing pattern).
   - Exposes `{ data, error, isLoading, mutate }` and auto-refresh interval.

3. **Create card component**  
   `src/components/dashboard/TopHubSignalCard.tsx`  
   - Uses `useTopHubSignal`.
   - Skeleton while loading; clear error state.
   - Severity badge (critical/high/medium/low) with accessible colors.
   - Opens detail drawer on click.

4. **Create detail drawer**  
   `src/components/dashboard/TopHubSignalDrawer.tsx`  
   - Slide-out panel with full anomaly list, context, and copyable JSON.
   - **Acknowledge** button (local dismiss or dashboard-level timestamp).
   - No “apply”, “execute”, or mutation controls.
   - Keyboard navigation, focus trap, ARIA labels.

5. **Wire into dashboard layout**  
   `src/pages/Dashboard.tsx`  
   - Insert `TopHubSignalCard` in top row with responsive stacking on mobile.

6. **Add types and tests**  
   - `src/types/signal.ts` for `TopHubSignalResponse` and `TopHubAnomaly`.
   - One simple component test for loading/error/empty states.

7. **Polish & accessibility**  
   - Color contrast for severity badges.
   - Keyboard navigation and ARIA attributes.
   - Stale-while-revalidate UX for auto-refresh.

---

### 3) Types

```ts
// src/types/signal.ts
export interface TopHubAnomaly {
  id: string;
  service: string;
  region: string;
  accountId: string;
  deltaUsd: number;
  severity: 'critical' | 'high' | 'medium' | 'low';
  startedAt: string; // ISO
  description: string;
}

export interface TopHubSignalResponse {
  hub: string;            // e.g. "MOC"
  severity: 'critical' | 'high' | 'medium' | 'low';
  windowStart: string;    // ISO
  windowEnd: string;      // ISO
  affectedAccounts: number;
  affectedServices: number;
  topAnomalies: TopHubAnomaly[];
  generatedAt: string;    // ISO
}
```

---

### 4) API helper

```ts
// src/lib/api/signals.ts
import { TopHubSignalResponse } from '../types/signal';

export async function fetchTopHubSignal(signal?: AbortSignal): Promise<TopHubSignalResponse> {
  const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal,
  });

  if (!res.ok) {
    const err: any = new Error(`Signal fetch failed: ${res.status}`);
    err.status = res.status;
    throw err;
  }

  return res.json();
}
```

---

### 5) Fetch hook (SWR-style example)

```ts
// src/hooks/useTopHubSignal.ts
import useSWR from 'swr';
import { fetchTopHubSignal } from '../lib/api/signals';
import { TopHubSignalResponse } from '../types/signal';

export function useTopHubSignal(intervalMs = 60_000) {
  const { data, error, isLoading, mutate } = useSWR<TopHubSignalResponse>(
    'top-hub-signal',
    fetchTopHubSignal,
    {
      refreshInterval: intervalMs,
      revalidateOnFocus: false,
      shouldRetryOnError: true,
      errorRetryCount: 3,
    }
  );

  return {
    data,
    error,
    isLoading,
    mutate,
  };
}
```

---

### 6) Card component (simplified)

```tsx
// src/components/dashboard/TopHubSignalCard.tsx
import { useTopHubSignal } from '../../hooks/useTopHubSignal';
import { TopHubSignalDrawer } from './TopHubSignalDrawer';
import { TopHubSignalResponse } from '../../types/signal';

const SEVERITY_COLORS = {
  critical: 'bg-red-600 text-white',
  high: 'bg-orange-500 text-white',
  medium: 'bg-yellow-400 text-gray-800',
  low: 'bg-gray-300 text-gray-700',
} as const;

export function TopHubSignalCard() {
  const { data, isLoading, error } = useTopHubSignal();
  const [drawerOpen, setDrawerOpen] = React.useState(false);

  if (isLoading) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50 animate-pulse">
        <div className="h-6 w-32 bg-gray-200 rounded mb-2"></div>
        <div className="h-4 w-24 bg-gray-200 rounded"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 border rounded-lg bg-red-50 text-red-700">
        {String(error)}
      </div>
    );
  }

  if (!data) return null;

  return (
    <>
      <button
        onClick={() => setDrawerOpen(true)}
        className="w-full text-left p-4 border rounded-lg bg-white shadow-sm hover:shadow transition-shadow"
        aria-label={`Top-Hub Signal: ${data.hub} ${data.severity}`}
      >
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="font-semibold text-gray-900">Top-Hub Signal</h3>
              <span className={`px-2 py-0.5 text-xs font-medium rounded ${SEVERITY_COLORS[data.severity]}`}>
                {data.severity.toUpperCase()}
              </span>
            </div>
            <p className="text-2xl font-bold text-gray-900 mt-1">{data.hub}</p>
            <p className="text-sm text-gray-500 mt-1">
              {data.affectedAccounts} accounts · {data.affectedServices} services
            </p>
          </div>
          <time className="text-xs text-gray-400" dateTime={data.generatedAt}>
            {new Date(data.generatedAt).toLocaleTimeString()}
          </time>
        </div>

        <div className="mt-3 space-y-1">
          {data.topAnomalies.slice(0, 3).map((a) => (

