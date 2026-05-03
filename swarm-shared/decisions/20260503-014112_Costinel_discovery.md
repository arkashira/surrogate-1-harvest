# Costinel / discovery

## Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the highest-connected hub and its actionable proposals. Resilient to missing backend with graceful fallback UI.  
**Timebox**: <2h  
**Stack**: React + TypeScript (existing Next.js app), Tailwind, optional SWR for data fetching.

### 1) Changes to make (highest-value incremental improvement)
- Add a persistent “Top-hub Signal” card to the Costinel dashboard.
- Fetch `/api/signal/top-hub` (or fallback to static JSON) and render:
  - Hub name + short description
  - Connection count (graph degree)
  - Top 3 actionable proposals (title, impact, due)
  - “View in Graph” link
- Graceful fallback UI when API/data is missing.
- No backend changes; keep data contract minimal.

### 2) File layout (assumed existing structure)
```
/opt/axentx/Costinel/
├─ app/
│  ├─ page.tsx                 # dashboard
│  └─ components/
│     └─ TopHubSignalPanel.tsx # new
├─ public/
│  └─ data/
│     └─ top-hub-fallback.json # new
└─ types/
   └─ signal.d.ts              # new
```

### 3) Implementation steps (ordered)

1. Add types (`types/signal.d.ts`)
2. Add fallback data (`public/data/top-hub-fallback.json`)
3. Create component (`components/TopHubSignalPanel.tsx`)
4. Mount component on dashboard (`app/page.tsx`)
5. Optional: add lightweight API route stub (if you want client fetch) — not required for <2h.

---

### Code snippets

#### `types/signal.d.ts`
```ts
export interface Proposal {
  id: string;
  title: string;
  impact: 'high' | 'medium' | 'low';
  due?: string; // ISO date
  url?: string;
}

export interface TopHub {
  hub: string;
  description: string;
  connections: number;
  proposals: Proposal[];
}
```

#### `public/data/top-hub-fallback.json`
```json
{
  "hub": "MOC",
  "description": "Multi-org cost governance hub — central policy & anomaly detection",
  "connections": 42,
  "proposals": [
    {
      "id": "p-001",
      "title": "Apply RI coverage to prod accounts",
      "impact": "high",
      "due": "2026-05-15",
      "url": "/proposals/p-001"
    },
    {
      "id": "p-002",
      "title": "Right-size over-provisioned EKS node groups",
      "impact": "medium",
      "due": "2026-05-10",
      "url": "/proposals/p-002"
    },
    {
      "id": "p-003",
      "title": "Tag enforcement for untagged resources",
      "impact": "high",
      "due": "2026-05-12",
      "url": "/proposals/p-003"
    }
  ]
}
```

#### `app/components/TopHubSignalPanel.tsx`
```tsx
'use client';

import useSWR from 'swr';
import { TopHub } from '@/types/signal';
import { ArrowRightIcon } from '@heroicons/react/20/solid';

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalPanel() {
  const { data, error } = useSWR<TopHub>('/api/signal/top-hub', fetcher, {
    fallbackData: require('@/public/data/top-hub-fallback.json'),
    revalidateOnMount: false,
  });

  const hub = data || (require('@/public/data/top-hub-fallback.json') as TopHub);
  const loading = !data && !error;
  const failed = !!error;

  const impactColor = (imp: string) => {
    switch (imp) {
      case 'high':
        return 'text-red-600 bg-red-50 ring-red-600/10';
      case 'medium':
        return 'text-amber-600 bg-amber-50 ring-amber-600/10';
      default:
        return 'text-emerald-600 bg-emerald-50 ring-emerald-600/10';
    }
  };

  return (
    <section
      className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-800 dark:bg-gray-900"
      aria-label="Top hub signal"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
              {hub.hub}
            </h2>
            <span className="inline-flex items-center rounded-md bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-400">
              {hub.connections} connections
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
            {hub.description}
          </p>
        </div>
        {failed && (
          <span className="text-xs text-amber-600" title="Using fallback data">
            fallback
          </span>
        )}
        {loading && <span className="text-xs text-gray-400">loading…</span>}
      </div>

      <div className="mt-4 space-y-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
          Actionable proposals
        </h3>
        <ul className="space-y-2" role="list">
          {hub.proposals.slice(0, 3).map((p) => (
            <li key={p.id} className="group flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100 group-hover:underline">
                  {p.title}
                </p>
                <div className="mt-1 flex items-center gap-2 text-xs">
                  <span
                    className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ${impactColor(
                      p.impact
                    )}`}
                  >
                    {p.impact}
                  </span>
                  {p.due && <span className="text-gray-500 dark:text-gray-400">Due {p.due}</span>}
                </div>
              </div>
              {p.url && (
                <a
                  href={p.url}
                  className="flex-shrink-0 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                  title="Open proposal"
                >
                  <ArrowRightIcon className="h-4 w-4" aria-hidden="true" />
                </a>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div className="mt-4">
        <a
          href="/graph?hub=MOC"
          className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
        >
          View in Graph
          <ArrowRightIcon className="h-3 w-3" aria-hidden="true" />
        </a>
      </div>
    </section>
  );
}
```

#### Mount on dashboard (`app/page.tsx`)
Locate the dashboard grid/layout and insert the panel near the top or in a prominent sidebar/column. Example snippet to add:

```tsx
import dynamic from 'next/dynamic';

// Client-only component (uses SWR)
