# Costinel / discovery

## Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the highest-connected hub and its actionable proposals. Resilient to missing backend with graceful fallback UI.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind (existing patterns).  
**Deliverable**: `TopHubSignalPanel` component + route mount in dashboard layout.

---

### 1) Design & Behavior (5 min)
- **Panel goal**: Show the most-connected hub (e.g., "MOC") with 3–5 top actionable proposals and a link to full context.
- **Data shape** (frontend contract):
  ```ts
  interface HubProposal {
    id: string;
    title: string;
    summary: string;
    impact: 'high' | 'medium' | 'low';
    actionUrl?: string; // optional deep link
  }
  interface TopHubPayload {
    hubName: string;
    hubSlug: string;
    description?: string;
    proposals: HubProposal[];
    updatedAt: string; // ISO
  }
  ```
- **States**: loading / data / empty / error (backend unreachable).
- **UI**: compact card with subtle accent border, impact badges, and a “View all in hub” link.

---

### 2) Implementation Steps (75–90 min)

#### A) Add component: `components/TopHubSignalPanel.tsx`
```tsx
// components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';

interface HubProposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  actionUrl?: string;
}

interface TopHubPayload {
  hubName: string;
  hubSlug: string;
  description?: string;
  proposals: HubProposal[];
  updatedAt: string;
}

const impactColors = {
  high: 'bg-red-100 text-red-800 border-red-200',
  medium: 'bg-amber-100 text-amber-800 border-amber-200',
  low: 'bg-green-100 text-green-800 border-green-200',
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetch('/api/top-hub')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json: TopHubPayload) => {
        if (!mounted) return;
        setData(json);
        setError(null);
      })
      .catch((err) => {
        if (!mounted) return;
        setError(err.message || 'Failed to load hub signal');
        setData(null);
      })
      .finally(() => {
        if (!mounted) return;
        setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-3 space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-10 animate-pulse rounded bg-gray-100" />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <p className="text-sm text-gray-500">
          Unable to load top-hub signal. Showing latest guidance.
        </p>
        <FallbackPanel />
      </div>
    );
  }

  if (!data || !data.proposals.length) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <p className="text-sm text-gray-500">No active hub signals at the moment.</p>
        <FallbackPanel />
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-base font-semibold text-gray-900">{data.hubName}</h3>
          {data.description && (
            <p className="mt-1 text-sm text-gray-600">{data.description}</p>
          )}
        </div>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700">
          Top hub
        </span>
      </div>

      <div className="mt-4 space-y-3">
        {data.proposals.map((p) => (
          <a
            key={p.id}
            href={p.actionUrl || '#'}
            className="block rounded-lg border border-gray-100 bg-gray-50 p-3 hover:border-gray-200 hover:bg-gray-100"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-gray-900">{p.title}</p>
                <p className="mt-1 line-clamp-2 text-xs text-gray-600">{p.summary}</p>
              </div>
              <span
                className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium ${
                  impactColors[p.impact]
                }`}
              >
                {p.impact}
              </span>
            </div>
          </a>
        ))}
      </div>

      <div className="mt-4 text-right">
        <a
          href={`/hubs/${data.hubSlug}`}
          className="text-sm font-medium text-blue-600 hover:underline"
        >
          View all in hub →
        </a>
      </div>

      <p className="mt-3 text-xs text-gray-400">
        Updated {new Date(data.updatedAt).toLocaleString()}
      </p>
    </div>
  );
}

function FallbackPanel() {
  const fallback: HubProposal[] = [
    {
      id: 'fallback-1',
      title: 'Review idle compute reservations',
      summary: 'Check for underutilized reserved instances across accounts.',
      impact: 'medium',
    },
    {
      id: 'fallback-2',
      title: 'Tagging compliance sweep',
      summary: 'Ensure cost allocation tags are present on new resources.',
      impact: 'low',
    },
  ];

  return (
    <div className="mt-3 space-y-2">
      {fallback.map((p) => (
        <div
          key={p.id}
          className="rounded-lg border border-gray-100 bg-gray-50 p-3"
        >
          <p className="text-sm font-medium text-gray-900">{p.title}</p>
          <p className="mt-1 text-xs text-gray-600">{p.summary}</p>
        </div>
      ))}
    </div>
  );
}
```

---

#### B) Add minimal API route stub: `pages/api/top-hub.ts` (Next.js)
```ts
// pages/api/top-hub.ts
import type { NextApiRequest, NextApiResponse } from 'next';

// Temporary stub until backend provides real endpoint.
// Returns deterministic sample to unblock frontend.
const sample: TopHubPayload = {
  hubName: 'MOC',
  hubSlug: 'moc',
  description: 'Multi-org cost
