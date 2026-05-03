# Costinel / frontend

## Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the highest-connected hub and its actionable proposals. Resilient to missing backend with graceful fallback UI.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind (existing patterns).  
**Entry point**: `src/components/dashboard/TopHubSignalPanel.tsx` (and related files).

---

### 1) Unified data model (resolved)
Use this single source-of-truth interface (combines clarity + completeness):

```ts
// src/lib/api.ts
export interface TopHubSignal {
  hub: {
    id: string;        // e.g. "MOC"
    name: string;      // e.g. "MOC"
    description: string;
    connections: number;
  };
  proposals: Array<{
    id: string;
    title: string;
    impact: 'high' | 'medium' | 'low';   // concrete enum for styling
    description: string;                  // short human-readable impact
    href?: string;                        // link to proposal detail
  }>;
}
```

---

### 2) File layout (create/modify)
```
src/
 ├─ components/
 │   └─ dashboard/
 │       ├─ TopHubSignalPanel.tsx
 │       └─ TopHubSignalPanelSkeleton.tsx
 ├─ hooks/
 │   └─ useTopHubSignal.ts
 ├─ lib/
 │   └─ api.ts
 └─ pages/
     └─ Dashboard.tsx   (import and mount panel)
```

---

### 3) Core code (merged + hardened)

#### `src/lib/api.ts`
```ts
const API_BASE = import.meta.env.VITE_API_BASE || '/api';

export interface TopHubSignal {
  hub: {
    id: string;
    name: string;
    description: string;
    connections: number;
  };
  proposals: Array<{
    id: string;
    title: string;
    impact: 'high' | 'medium' | 'low';
    description: string;
    href?: string;
  }>;
}

export async function fetchTopHubSignal(): Promise<TopHubSignal | null> {
  try {
    const res = await fetch(`${API_BASE}/top-hub/signal`, {
      headers: { Accept: 'application/json' },
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  } catch {
    // Graceful fallback: try static CDN/public JSON if available
    try {
      const fallback = await fetch('/data/fallback-top-hub.json');
      if (fallback.ok) return fallback.json();
    } catch {
      // ignore
    }
    return null;
  }
}
```

---

#### `src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import { fetchTopHubSignal, TopHubSignal } from '../lib/api';

export function useTopHubSignal(pollIntervalMs = 0) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await fetchTopHubSignal();
      setData(result);
      setError(null);
    } catch (err) {
      setError(err as Error);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (pollIntervalMs > 0) {
      const id = setInterval(load, pollIntervalMs);
      return () => clearInterval(id);
    }
  }, [pollIntervalMs, load]);

  return { data, loading, error, refetch: load };
}
```

---

#### `src/components/dashboard/TopHubSignalPanelSkeleton.tsx`
```tsx
import React from 'react';

export default function TopHubSignalPanelSkeleton() {
  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-full bg-muted animate-pulse" />
        <div className="space-y-2 flex-1">
          <div className="h-4 w-28 rounded bg-muted animate-pulse" />
          <div className="h-3 w-40 rounded bg-muted animate-pulse" />
        </div>
      </div>

      <div className="mt-4 space-y-3">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i} className="flex items-start gap-3">
            <div className="h-2 w-2 mt-2 rounded-full bg-muted shrink-0" />
            <div className="space-y-2 flex-1">
              <div className="h-4 w-3/4 rounded bg-muted animate-pulse" />
              <div className="h-3 w-1/2 rounded bg-muted animate-pulse" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

---

#### `src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { ExternalLink } from 'lucide-react';
import { TopHubSignal } from '../../lib/api';
import TopHubSignalPanelSkeleton from './TopHubSignalPanelSkeleton';

interface Props {
  signal: TopHubSignal | null;
  loading?: boolean;
}

const impactColors = {
  high: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  medium: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  low: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
} as const;

export default function TopHubSignalPanel({ signal, loading }: Props) {
  if (loading) return <TopHubSignalPanelSkeleton />;

  if (!signal) {
    return (
      <div className="rounded-lg border bg-card p-4 text-center text-sm text-muted-foreground">
        Top-hub signal unavailable.
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10 text-primary font-bold">
            {signal.hub.id.slice(0, 2).toUpperCase()}
          </div>
          <div>
            <h3 className="font-semibold">{signal.hub.name}</h3>
            <p className="text-xs text-muted-foreground">{signal.hub.description}</p>
          </div>
        </div>
        <span className="text-xs text-muted-foreground shrink-0">
          {signal.hub.connections} connections
        </span>
      </div>

      <div className="mt-4 space-y-2">
        <h4 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
          Actionable proposals
        </h4>
        {signal.proposals.length === 0 ? (
          <p className="text-sm text-muted-foreground">No proposals at this time.</p>
        ) : (
          <ul className="space-y-2">
            {signal.proposals.map((p) => (
              <li key={p.id} className="flex items-start gap-2 text-sm">
                <span className={`mt-1.5 h-1.5 w-1.5 rounded-full shrink-0 ${
                  impactColors[p.impact]
                }`} />
                <div className="flex-1 min-w-0">
                  <p className="font-medium truncate">{p
