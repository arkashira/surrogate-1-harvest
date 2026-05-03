# Costinel / backend

## Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: <2h (frontend-only, no backend changes).

### Why this now
- Follows past pattern: review the most-connected hub (e.g., "MOC") before planning tasks (#knowledge-rag #graph #hub).
- Adds immediate operational value to Costinel dashboards by surfacing graph-derived context without touching backend or data pipelines.
- Read-only, zero risk, and can be built entirely in the FE layer by reusing existing graph metadata (or a lightweight local JSON if not yet exposed).

---

### Implementation Plan (1h 30m)

1. **Locate dashboard layout** (10m)  
   Identify where to insert the card (likely near cost analytics header or sidebar). Prefer a prominent but non-blocking slot.

2. **Create `TopHubSignalCard` component** (30m)  
   - Accepts `graphMeta` prop: `{ hubs: Array<{id, label, connections, signals: Array<{label, value, ts, href}>}> }`
   - Picks hub with max `connections`.
   - Renders:
     - Hub label + connection count
     - Up to 3 contextual signals (label + short value)
     - Optional link to full context (if `href` present)
   - Skeleton/loading state for async cases.

3. **Add lightweight data adapter** (15m)  
   - If backend already exposes `/api/knowledge-rag/top-hub`, call it (GET, no body).
   - Else, use a static JSON at `public/data/top-hub.json` updated by ops (zero BE work).  
   - Normalize to `{ hub, signals }`.

4. **Wire into dashboard** (15m)  
   - Import and mount `TopHubSignalCard` in the dashboard view.
   - Fetch on mount (or server-side if Next.js).
   - Add refresh interval (e.g., 300s) or manual refresh button.

5. **Styling & polish** (15m)  
   - Use existing design tokens (shadows, border radius, color roles).
   - Ensure responsive (col-span on grid, full width on mobile).
   - Add subtle icon (hub/network) and accessible labels.

6. **Tests & smoke** (5m)  
   - Verify render with empty/missing data.
   - Verify link clicks open in new tab when `href` present.

---

### Code Snippets

#### Component: `TopHubSignalCard.tsx`
```tsx
import { useEffect, useState } from 'react';
import { ExternalLink } from 'lucide-react';

type Signal = { label: string; value: string | number; ts?: string; href?: string };
type Hub = { id: string; label: string; connections: number; signals: Signal[] };
type GraphMeta = { hubs: Hub[] };

type TopHubSignalCardProps = {
  graphMeta?: GraphMeta;
  loading?: boolean;
  onRefresh?: () => void;
};

export function TopHubSignalCard({ graphMeta, loading, onRefresh }: TopHubSignalCardProps) {
  const topHub = graphMeta?.hubs?.reduce((best, cur) =>
    cur.connections > best.connections ? cur : best
  ) || null;

  if (loading || !graphMeta) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-sm animate-pulse">
        <div className="h-5 w-32 bg-muted rounded mb-3" />
        <div className="h-4 w-24 bg-muted rounded mb-1" />
        <div className="space-y-2">
          <div className="h-4 w-full bg-muted rounded" />
          <div className="h-4 w-5/6 bg-muted rounded" />
        </div>
      </div>
    );
  }

  if (!topHub) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-sm text-muted-foreground">
        No hub data available.
      </div>
    );
  }

  const topSignals = topHub.signals.slice(0, 3);

  return (
    <div className="rounded-xl border bg-card p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h3 className="font-semibold text-base">{topHub.label}</h3>
          <p className="text-xs text-muted-foreground">
            {topHub.connections} connections — top hub
          </p>
        </div>
        {onRefresh && (
          <button
            onClick={onRefresh}
            className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
            type="button"
          >
            Refresh
          </button>
        )}
      </div>

      <ul className="space-y-2" aria-label="Contextual signals">
        {topSignals.map((s, i) => (
          <li key={i} className="flex items-start gap-2 text-sm">
            <span className="flex-1 truncate font-medium">{s.label}</span>
            <span className="text-muted-foreground shrink-0">{s.value}</span>
            {s.href && (
              <a
                href={s.href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-muted-foreground hover:text-foreground shrink-0"
                title="Open context"
              >
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

#### Data fetch hook (optional): `useTopHub.ts`
```ts
import useSWR from 'swr';

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export function useTopHub(enabled = true) {
  // Prefer backend endpoint; fallback to static JSON
  const { data, error, isLoading, mutate } = useSWR<{ hubs: any[] }>(
    enabled ? '/api/knowledge-rag/top-hub' : null,
    fetcher,
    { refreshInterval: 300_000, revalidateOnFocus: false }
  );

  return {
    graphMeta: data,
    loading: isLoading,
    error,
    refresh: () => mutate(),
  };
}
```

#### Usage in dashboard page
```tsx
import { TopHubSignalCard } from '@/components/TopHubSignalCard';
import { useTopHub } from '@/hooks/useTopHub';

export default function DashboardPage() {
  const { graphMeta, loading, refresh } = useTopHub(true);

  return (
    <div className="p-6 space-y-6">
      <div className="grid gap-6 md:grid-cols-3">
        <div className="md:col-span-1">
          <TopHubSignalCard graphMeta={graphMeta} loading={loading} onRefresh={refresh} />
        </div>
        {/* other cost cards */}
      </div>
    </div>
  );
}
```

#### Static fallback (if no backend)
Create `public/data/top-hub.json` (ops-maintained):
```json
{
  "hubs": [
    {
      "id": "MOC",
      "label": "MOC",
      "connections": 128,
      "signals": [
        { "label": "Cost anomaly", "value": "+18% WoW", "href": "/anomalies/123" },
        { "label": "Top service", "value": "EKS", "href": "/services/eks" },
        { "label": "Recommendation", "value": "RI 1-yr 42% coverage", "href": "/ri/coverage" }
      ]
    }
  ]
}
```

---

### Acceptance Criteria
- [ ] Card renders top hub by connection count.
- [ ] Shows up to 3 contextual signals with labels/values.
- [ ] Links open in new tab when present.
- [ ] Handles loading, empty,
