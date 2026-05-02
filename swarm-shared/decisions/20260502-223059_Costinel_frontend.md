# Costinel / frontend

## Highest-Value Incremental Improvement (<2h)

**Improvement:** Add a deterministic, read-only “Today’s strongest cost-anomaly signal” widget to the Costinel frontend that calls `GET /api/v1/cost-anomaly/signal` and renders a single actionable card with service, delta, and context.

**Why this:**  
- Directly exposes the new backend endpoint from prior decisions.  
- Zero side effects, no execution (aligns with “Sense + Signal — ไม่ Execute”).  
- Visible user value immediately (real-time anomaly visibility).  
- Implementable in <2h with minimal code.

---

## Implementation Plan

1. **Create component** `AnomalySignalCard.tsx`  
   - Fetch `/api/v1/cost-anomaly/signal` on mount (SWR or TanStack Query).  
   - Handle loading / error / empty states.  
   - Render: service name, delta %, severity badge, time window, short insight.

2. **Add route/page integration**  
   - Place widget at top of dashboard (index page) so it’s seen first.

3. **Styling**  
   - Use existing design tokens and Tailwind classes (match dashboard cards).  
   - Color by severity: green/amber/red.

4. **Types**  
   - Add minimal shared type `CostAnomalySignal` in `types/api.ts`.

5. **Build + smoke test**  
   - `npm run build` and verify no TS errors.  
   - Run dev server and confirm card appears and loads data.

---

## Code Snippets

### 1) Type definition (`src/types/api.ts`)

```ts
export interface CostAnomalySignal {
  service: string;
  accountId: string;
  region: string;
  deltaPercent: number;
  baseline: number;
  current: number;
  severity: 'low' | 'medium' | 'high' | 'critical';
  window: string; // e.g. "last 24h vs trailing 7d baseline"
  insight: string;
  timestamp: string; // ISO
}
```

---

### 2) Component (`src/components/AnomalySignalCard.tsx`)

```tsx
import { useQuery } from '@tanstack/react-query';
import { CostAnomalySignal } from '@/types/api';
import { AlertTriangle, TrendingUp, TrendingDown } from 'lucide-react';

async function fetchSignal(): Promise<CostAnomalySignal | null> {
  const res = await fetch('/api/v1/cost-anomaly/signal');
  if (!res.ok) {
    if (res.status === 404) return null;
    throw new Error('Failed to fetch anomaly signal');
  }
  return res.json();
}

export function AnomalySignalCard() {
  const { data, isLoading, error } = useQuery<CostAnomalySignal | null>({
    queryKey: ['cost-anomaly-signal'],
    queryFn: fetchSignal,
    staleTime: 60_000,
    refetchInterval: 300_000,
  });

  if (isLoading) {
    return (
      <div className="animate-pulse rounded-xl bg-card p-6 shadow-sm">
        <div className="h-5 w-32 rounded bg-muted" />
        <div className="mt-4 h-8 w-48 rounded bg-muted" />
      </div>
    );
  }

  if (error || !data) {
    return null; // silent fail when no signal
  }

  const isUp = data.deltaPercent > 0;
  const severityColors = {
    low: 'border-l-green-500 bg-green-50/50 text-green-700',
    medium: 'border-l-amber-500 bg-amber-50/50 text-amber-700',
    high: 'border-l-orange-600 bg-orange-50/50 text-orange-800',
    critical: 'border-l-red-600 bg-red-50/50 text-red-800',
  };

  return (
    <div
      className={`rounded-xl border-l-4 bg-card p-6 shadow-sm transition ${severityColors[data.severity]}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <div
            className={`rounded-full p-2 ${
              isUp ? 'bg-red-100 text-red-600' : 'bg-green-100 text-green-600'
            }`}
          >
            {isUp ? <TrendingUp size={20} /> : <TrendingDown size={20} />}
          </div>
          <div>
            <p className="text-sm font-medium text-muted-foreground">
              Strongest cost anomaly today
            </p>
            <p className="text-xl font-semibold">{data.service}</p>
            <p className="text-sm text-muted-foreground">
              {data.region} · {data.accountId}
            </p>
          </div>
        </div>

        <div className="text-right">
          <p
            className={`text-2xl font-bold ${
              isUp ? 'text-red-700' : 'text-green-700'
            }`}
          >
            {isUp ? '+' : ''}
            {data.deltaPercent.toFixed(1)}%
          </p>
          <p className="text-sm text-muted-foreground">{data.window}</p>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2 text-sm">
        <AlertTriangle size={16} className="shrink-0" />
        <span className="text-muted-foreground">{data.insight}</span>
      </div>

      <div className="mt-4 text-xs text-muted-foreground">
        Baseline: {data.baseline.toLocaleString()} | Current:{' '}
        {data.current.toLocaleString()}
      </div>
    </div>
  );
}
```

---

### 3) Dashboard integration (`src/pages/dashboard/index.tsx`)

```tsx
import { AnomalySignalCard } from '@/components/AnomalySignalCard';

export default function DashboardPage() {
  return (
    <main className="mx-auto max-w-7xl space-y-6 p-6">
      <AnomalySignalCard />
      {/* existing dashboard widgets below */}
      {/* ... */}
    </main>
  );
}
```

---

### 4) Build & smoke test

```bash
npm run build
# verify no TS errors

npm run dev
# open dashboard, confirm card renders and loads signal
```

---

**Estimated effort:** ~90 minutes (types + component + integration + styling + smoke test).
