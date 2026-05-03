# Costinel / frontend

## Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**  
- Frontend-only, read-only panel.  
- Surface the most-connected hub (e.g., “MOC”) and actionable proposals.  
- Resilient to missing backend: graceful fallback UI + local mock.  
- Timebox: <2h.

**Tech stack**  
- React + TypeScript (existing app).  
- Tailwind for styling.  
- `fetch` + `AbortController` for resilient data fetching.  
- Local mock JSON for dev/fallback.

**Files to add/modify**  
- `src/components/TopHubSignalPanel.tsx` (new)  
- `src/mocks/topHubMock.json` (new)  
- `src/pages/Dashboard.tsx` (import + mount panel)  
- `src/types/topHub.ts` (new)

---

### 1) Types (`src/types/topHub.ts`)

```ts
export interface Proposal {
  id: string;
  title: string;
  description: string;
  impact: 'high' | 'medium' | 'low';
  confidence: number; // 0..1
  action?: string; // optional CTA label (read-only)
  tags?: string[];
}

export interface HubInsight {
  hub: string;
  rank: number;
  connections: number;
  summary: string;
  proposals: Proposal[];
  updatedAt: string; // ISO
}

export interface TopHubResponse {
  data: HubInsight | null;
  meta: {
    source: 'api' | 'cache' | 'mock';
    fetchedAt?: string;
  };
}
```

---

### 2) Mock data (`src/mocks/topHubMock.json`)

```json
{
  "data": {
    "hub": "MOC",
    "rank": 1,
    "connections": 42,
    "summary": "Most-connected hub with cross-account cost anomalies and idle resource clusters.",
    "proposals": [
      {
        "id": "p-001",
        "title": "Right-size idle EKS node groups",
        "description": "Detected 3 node groups with <15% avg CPU over 14 days. Estimated savings $2.4k/mo.",
        "impact": "high",
        "confidence": 0.86,
        "tags": ["eks", "compute", "savings"]
      },
      {
        "id": "p-002",
        "title": "Convert dev RDS to reserved instances",
        "description": "Steady-state dev workloads eligible for 1yr No Upfront RIs. Coverage 68%.",
        "impact": "medium",
        "confidence": 0.72,
        "tags": ["rds", "ri", "coverage"]
      }
    ],
    "updatedAt": "2026-05-03T08:00:00.000Z"
  },
  "meta": {
    "source": "mock"
  }
}
```

---

### 3) Panel component (`src/components/TopHubSignalPanel.tsx`)

```tsx
import React, { useEffect, useState, useCallback } from 'react';
import type { HubInsight, TopHubResponse } from '../types/topHub';
import mockData from '../mocks/topHubMock.json';

const API_URL = '/api/top-hub'; // adjust to your backend route
const FALLBACK_DELAY = 3000; // ms before showing fallback if API hangs

const impactColor = (impact: string) => {
  switch (impact) {
    case 'high':
      return 'text-red-600 bg-red-50 border-red-200';
    case 'medium':
      return 'text-amber-600 bg-amber-50 border-amber-200';
    default:
      return 'text-green-600 bg-green-50 border-green-200';
  }
};

const TopHubSignalPanel: React.FC = () => {
  const [insight, setInsight] = useState<HubInsight | null>(null);
  const [source, setSource] = useState<'api' | 'cache' | 'mock'>('mock');
  const [loading, setLoading] = useState(true);

  const fetchWithFallback = useCallback(async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);

    try {
      const res = await fetch(API_URL, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        signal: controller.signal,
        cache: 'no-store',
      });
      clearTimeout(timeout);

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: TopHubResponse = await res.json();
      if (json?.data) {
        setInsight(json.data);
        setSource(json.meta?.source || 'api');
        return;
      }
      throw new Error('Empty payload');
    } catch (err) {
      // graceful fallback
      console.warn('Top-hub API unavailable, using mock:', err);
      setInsight((mockData as TopHubResponse).data);
      setSource('mock');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchWithFallback();
  }, [fetchWithFallback]);

  if (loading && !insight) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 animate-pulse rounded-full bg-gray-200" />
          <div className="space-y-2">
            <div className="h-4 w-32 animate-pulse rounded bg-gray-200" />
            <div className="h-3 w-24 animate-pulse rounded bg-gray-100" />
          </div>
        </div>
      </div>
    );
  }

  if (!insight) return null;

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      {/* Header */}
      <div className="mb-4 flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-semibold text-gray-900">Top Hub: {insight.hub}</h3>
            <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700">
              Rank #{insight.rank}
            </span>
          </div>
          <p className="text-sm text-gray-500">{insight.connections} connections</p>
        </div>
        <span className="text-xs text-gray-400">
          Source: {source} • {new Date(insight.updatedAt).toLocaleDateString()}
        </span>
      </div>

      {/* Summary */}
      <p className="mb-4 text-sm text-gray-700">{insight.summary}</p>

      {/* Proposals */}
      <div className="space-y-3">
        <h4 className="text-sm font-semibold text-gray-900">Actionable Proposals</h4>
        {insight.proposals.map((p) => (
          <div
            key={p.id}
            className={`rounded border-l-4 bg-opacity-50 p-3 ${impactColor(p.impact)}`}
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="text-sm font-semibold text-gray-900">{p.title}</p>
                <p className="mt-0.5 text-xs text-gray-600">{p.description}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {p.tags?.map((t) => (
                    <span
                      key={t}
                      className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-600"
                    >
                      {t}
                    </
