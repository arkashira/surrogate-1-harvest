# Costinel / quality

**Final, merged implementation plan**  
(Combines strongest parts of Candidates 1 + 2, resolves contradictions, and favors correctness + concrete actionability.)

---

## Scope & Value
- **Frontend-only, read-only panel** (≤2h).  
- Surface the **most-connected hub** (e.g., “MOC”) and **actionable proposals** from the knowledge graph.  
- **Resilient to missing backend**: graceful fallback UI + local sample data + localStorage cache.  
- **Zero breaking changes**; integrates into existing dashboard layout.  
- **Framework-agnostic guidance** below; pick React or Vue path depending on your app.

---

## Why this ships highest value in ≤2h
- No backend, infra, or schema migrations.  
- Reuses existing design tokens and components.  
- Immediate user-facing insight (hub + proposals) that reinforces “Sense + Signal” philosophy.

---

## File changes (React path)
1. `src/components/TopHubSignalPanel.tsx` — new component.  
2. `src/pages/Dashboard.tsx` — mount panel in sidebar/summary section.  
3. `src/lib/graphApi.ts` — lightweight fetcher with CDN/local fallback + localStorage cache.  
4. `src/lib/sampleData/topHubSample.json` — local sample for resilient UI.  
5. `src/types/graph.ts` — add minimal types.

## File changes (Vue path)
1. `src/components/CostinelTopHubPanel.vue` — new component.  
2. `src/views/Dashboard.vue` — import and mount panel in primary grid.  
3. `src/composables/useTopHub.ts` — composable to fetch + cache data.  
4. `src/data/sample-top-hub.json` — local sample data.  
5. `src/types/graph.ts` — shared minimal types.

---

## Shared types (use in both paths)
```ts
// src/types/graph.ts
export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  tags?: string[];
}

export interface HubNode {
  id: string;
  label: string;
  description?: string;
  connectionCount: number;
}

export interface TopHubData {
  hub: HubNode;
  proposals: Proposal[];
}
```

---

## API utility (React) — resilient + cached
```ts
// src/lib/graphApi.ts
import type { TopHubData } from '../types/graph';

const CACHE_KEY = 'costinel_topHub_cache';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5m

function getCached(): TopHubData | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { data, ts } = JSON.parse(raw);
    if (Date.now() - ts > CACHE_TTL_MS) return null;
    return data as TopHubData;
  } catch {
    return null;
  }
}

function setCached(data: TopHubData) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ data, ts: Date.now() }));
  } catch {
    // ignore storage failures
  }
}

export async function fetchTopHubAndProposals(): Promise<TopHubData> {
  const cached = getCached();
  if (cached) return cached;

  // Replace URL with your real endpoint when available
  const res = await fetch('/api/graph/top-hub-signals', {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    // short timeout behavior via AbortController in production
  }).catch(() => {
    throw new Error('Network fetch failed');
  });

  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }

  const payload = (await res.json()) as TopHubData;
  // Basic runtime validation (lightweight)
  if (!payload?.hub?.id || !Array.isArray(payload.proposals)) {
    throw new Error('Invalid payload shape');
  }

  setCached(payload);
  return payload;
}
```

---

## React component (final)
```tsx
// src/components/TopHubSignalPanel.tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHubAndProposals } from '../lib/graphApi';
import type { TopHubData, Proposal } from '../types/graph';
import './TopHubSignalPanel.css'; // optional: keep styles minimal

const SAMPLE_DATA: TopHubData = {
  hub: {
    id: 'MOC',
    label: 'MOC',
    description: 'Mission Operations Center — central coordination hub',
    connectionCount: 128,
  },
  proposals: [
    {
      id: 'prop-1',
      title: 'Standardize MOC change windows',
      summary: 'Align weekly maintenance windows to reduce cross-team incidents',
      impact: 'high',
      tags: ['governance', 'availability'],
    },
    {
      id: 'prop-2',
      title: 'Introduce MOC runbook automation',
      summary: 'Automate common runbook steps for faster incident response',
      impact: 'medium',
      tags: ['automation', 'ops'],
    },
  ],
};

const impactColor = (impact: string) => {
  switch (impact) {
    case 'high':
      return 'bg-red-50 text-red-700 border-red-200';
    case 'medium':
      return 'bg-amber-50 text-amber-700 border-amber-200';
    default:
      return 'bg-blue-50 text-blue-700 border-blue-200';
  }
};

export const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    fetchTopHubAndProposals()
      .then((res) => {
        if (!mounted) return;
        setData(res);
        setError(null);
      })
      .catch((err) => {
        if (!mounted) return;
        // Log for debugging but show graceful fallback
        console.warn('[TopHubSignalPanel] fetch failed, using sample', err);
        setData(SAMPLE_DATA);
        setError('Using local sample data');
      })
      .finally(() => {
        if (!mounted) return;
        setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  const hub = data?.hub ?? SAMPLE_DATA.hub;
  const proposals = data?.proposals ?? SAMPLE_DATA.proposals;

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-4 space-y-3">
          <div className="h-4 w-full animate-pulse rounded bg-gray-100" />
          <div className="h-4 w-5/6 animate-pulse rounded bg-gray-100" />
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="border-b border-gray-100 px-4 py-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold text-gray-900">Top Hub</h3>
            <p className="text-lg font-medium text-gray-900">{hub.label}</p>
          </div>
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
            {hub.connectionCount} connections
          </span>
        </div>
        {hub.description && (
          <p className="mt-1 px-4 text-xs text-gray-500">{hub.description}</p>
        )}
