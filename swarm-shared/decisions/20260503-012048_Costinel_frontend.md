# Costinel / frontend

## Highest-value incremental improvement (<2h)

**Add a Top-hub Signal Panel to the Costinel dashboard**  
- Surfaces the most-connected hub (e.g., "MOC") and actionable proposals from the knowledge graph.  
- Read-only: Sense + Signal (no execution).  
- Uses `/api/v1/sense/top-hub-signal` (or falls back to local sample data if endpoint unavailable).  
- Fits existing frontend patterns and can ship immediately.

---

## Implementation plan

1. **Create API client utility** (`src/lib/api/sense.ts`)  
   - Expose `fetchTopHubSignal()` with graceful fallback to bundled sample data.

2. **Create panel component** (`src/components/dashboard/TopHubSignalPanel.tsx`)  
   - Card layout consistent with existing dashboard widgets.  
   - Shows hub name, short insight, and list of proposals with metadata (impact, confidence).  
   - Skeleton loader + empty/error states.

3. **Wire into main dashboard** (`src/pages/Dashboard.tsx` or equivalent)  
   - Insert panel near top of the dashboard grid (high visibility).  
   - Fetch on mount with 30–60s refresh interval (polling) or via SWR/React Query if already used.

4. **Add types** (`src/types/sense.ts`)  
   - Minimal, strict types for payload shape.

5. **Tests & lint**  
   - Quick smoke test in dev; no e2e required for this incremental.

Estimated effort: ~90–110 minutes.

---

## Code snippets

### 1) Types

```ts
// src/types/sense.ts
export interface Proposal {
  id: string;
  title: string;
  description: string;
  impact: 'high' | 'medium' | 'low';
  confidence: number; // 0..1
  action?: string; // optional human action hint
}

export interface TopHubSignal {
  hub: string;
  insight: string;
  generatedAt: string; // ISO
  proposals: Proposal[];
}
```

### 2) API client with fallback

```ts
// src/lib/api/sense.ts
import type { TopHubSignal } from '@/types/sense';

const SAMPLE: TopHubSignal = {
  hub: 'MOC',
  insight:
    'Multi-account orchestration cluster shows recurring idle capacity during off-peak windows; consolidating workloads could reduce compute spend by ~18%.',
  generatedAt: new Date().toISOString(),
  proposals: [
    {
      id: 'p-1',
      title: 'Right-size node pools for nightly batch',
      description: 'Scale down non-critical node pools between 20:00–06:00 UTC.',
      impact: 'high',
      confidence: 0.82,
      action: 'Schedule automated scale-down policy',
    },
    {
      id: 'p-2',
      title: 'Increase RI coverage for steady-state services',
      description: 'Convert 40% of on-demand baseline to 1-year RIs.',
      impact: 'medium',
      confidence: 0.74,
      action: 'Run RI purchase proposal workflow',
    },
  ],
};

export async function fetchTopHubSignal(): Promise<TopHubSignal> {
  try {
    const res = await fetch('/api/v1/sense/top-hub-signal', {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
      // include credentials if your app uses them
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch {
    // Graceful fallback so UI remains functional during rollout
    return SAMPLE;
  }
}
```

### 3) Panel component

```tsx
// src/components/dashboard/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal } from '@/lib/api/sense';
import type { TopHubSignal, Proposal } from '@/types/sense';

const impactColor = (impact: Proposal['impact']) => {
  switch (impact) {
    case 'high':
      return 'text-red-600 bg-red-50 border-red-200';
    case 'medium':
      return 'text-amber-600 bg-amber-50 border-amber-200';
    default:
      return 'text-green-600 bg-green-50 border-green-200';
  }
};

export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetchTopHubSignal().then((data) => {
      if (mounted) {
        setSignal(data);
        setLoading(false);
      }
    });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-white p-5 shadow-sm">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-200 mb-3" />
        <div className="h-4 w-full animate-pulse rounded bg-gray-100 mb-2" />
        <div className="h-4 w-5/6 animate-pulse rounded bg-gray-100" />
      </div>
    );
  }

  if (!signal) {
    return (
      <div className="rounded-lg border bg-white p-5 shadow-sm">
        <p className="text-sm text-gray-500">Signal unavailable.</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-base font-semibold text-gray-900">Top-hub: {signal.hub}</h3>
          <p className="text-xs text-gray-500 mt-1">
            Updated {new Date(signal.generatedAt).toLocaleString()}
          </p>
        </div>
      </div>

      <p className="text-sm text-gray-700 mb-4">{signal.insight}</p>

      <div className="space-y-3">
        <h4 className="text-sm font-medium text-gray-900">Proposals</h4>
        {signal.proposals.length === 0 ? (
          <p className="text-sm text-gray-500">No proposals available.</p>
        ) : (
          <ul className="space-y-2" role="list">
            {signal.proposals.map((p) => (
              <li
                key={p.id}
                className="flex items-start gap-3 p-3 rounded border text-sm"
              >
                <span
                  className={`mt-0.5 px-2 py-0.5 rounded text-xs font-medium border ${impactColor(
                    p.impact
                  )}`}
                >
                  {p.impact}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-gray-900 truncate">{p.title}</p>
                  <p className="text-gray-600 text-xs mt-0.5 line-clamp-2">
                    {p.description}
                  </p>
                  <div className="flex items-center gap-2 mt-2 text-xs text-gray-500">
                    <span>Confidence {(p.confidence * 100).toFixed(0)}%</span>
                    {p.action && <span>• {p.action}</span>}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
```

### 4) Add to dashboard grid

```tsx
// src/pages/Dashboard.tsx (or wherever the main grid lives)
import TopHubSignalPanel from '@/components/dashboard/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <main className="p-6 space-y-6">
      {/* Existing header/controls */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

