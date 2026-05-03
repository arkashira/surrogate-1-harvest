# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data (bypasses HF API auth/rate-limits at runtime). Ships in <2h.

### 1) Architecture & Data Flow (CDN-first with resilient fallback)
- **Primary source**: CDN JSON (no Authorization, no HF API rate limits).  
  Published path: `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/knowledge-rag/top-hub/moc-top3.json`
- **Local fallback**: `public/data/top-hub-signal.json` (committed to repo) served via app CDN if remote fetch fails.
- **Refresh**: 5-minute polling (configurable) with stale-while-revalidate UX.
- **Scope boundary**: Read-only panel; no mutations or execute actions.

### 2) Data Contract (unified)

File: `public/data/top-hub-signal.json` (local fallback) and CDN equivalent.

```json
{
  "hub": "MOC",
  "hubLabel": "Mission Operations Center",
  "updatedAt": "2026-05-03T02:20:57Z",
  "proposals": [
    {
      "id": "moc-ri-2026-05",
      "title": "Increase RI coverage for prod us-east-1",
      "description": "Current coverage 62% → target 85% saves $42k/mo.",
      "signal": "Compute >70% on-demand in us-east-1 for 30d",
      "action": "Purchase 1yr No Upfront RIs for m5.large/m5.xlarge fleet",
      "impact": "high",
      "signalScore": 94,
      "confidence": 0.87,
      "savingsUSD": 42000,
      "actions": ["run-ri-simulator", "create-proposal"],
      "tags": ["aws", "ri", "cost-savings"]
    },
    {
      "id": "moc-snapshots-2026-05",
      "title": "Orphaned EBS Snapshots",
      "description": "140 snapshots >90d old, unattached volumes.",
      "signal": "140 snapshots >90d old, unattached volumes",
      "action": "Lifecycle policy: retain 30d, then archive to Glacier",
      "impact": "medium",
      "signalScore": 88,
      "confidence": 0.92,
      "savingsUSD": 8500,
      "actions": ["create-lifecycle-policy"],
      "tags": ["aws", "ebs", "snapshots"]
    },
    {
      "id": "moc-downsize-2026-05",
      "title": "Over-provisioned DB Instances",
      "description": "RDS CPU <20% peak for 14d across 6 instances.",
      "signal": "RDS CPU <20% peak for 14d across 6 instances",
      "action": "Downsize db.t3.large → db.t3.medium with burstable credits",
      "impact": "medium",
      "signalScore": 81,
      "confidence": 0.78,
      "savingsUSD": 15600,
      "actions": ["create-downsize-plan"],
      "tags": ["aws", "rds", "rightsize"]
    }
  ]
}
```

### 3) Types

File: `src/types/knowledge.ts`

```ts
export type Impact = 'high' | 'medium' | 'low';

export interface TopHubProposal {
  id: string;
  title: string;
  description?: string;
  signal: string;
  action: string;
  impact: Impact;
  signalScore: number;
  confidence: number;
  savingsUSD: number;
  actions: string[];
  tags: string[];
}

export interface TopHubSignal {
  hub: string;
  hubLabel: string;
  updatedAt: string;
  proposals: TopHubProposal[];
}
```

### 4) Hook: resilient CDN-first fetcher

File: `src/hooks/useTopHubSignal.ts`

```ts
import { useEffect, useState, useCallback } from 'react';
import { TopHubSignal } from '../types/knowledge';

const REMOTE_CDN =
  'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/knowledge-rag/top-hub/moc-top3.json';
const LOCAL_FALLBACK = '/data/top-hub-signal.json';
const REFRESH_MS = 5 * 60 * 1000; // 5m

export function useTopHubSignal(pollInterval = REFRESH_MS) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async (url: string) => {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Failed to fetch top-hub signal: ${res.status}`);
    return res.json() as Promise<TopHubSignal>;
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // Try remote CDN first
      const remote = await fetchData(REMOTE_CDN);
      setData(remote);
      setError(null);
    } catch (e) {
      try {
        // Fallback to local bundled file
        const local = await fetchData(LOCAL_FALLBACK);
        setData(local);
        setError(null);
      } catch (e2) {
        setError((e2 as Error).message);
      }
    } finally {
      setLoading(false);
    }
  }, [fetchData]);

  useEffect(() => {
    load();
    const id = setInterval(load, pollInterval);
    return () => clearInterval(id);
  }, [load, pollInterval]);

  return { data, loading, error, refetch: load };
}
```

### 5) Panel component

File: `src/components/TopHubSignalPanel.tsx`

```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import { format } from 'date-fns';

const impactColor = (impact: string) => {
  switch (impact) {
    case 'high':
      return 'bg-red-100 text-red-800 border-red-200';
    case 'medium':
      return 'bg-amber-100 text-amber-800 border-amber-200';
    default:
      return 'bg-green-100 text-green-800 border-green-200';
  }
};

export const TopHubSignalPanel: React.FC = () => {
  const { data, loading, error, refetch } = useTopHubSignal();

  if (loading) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50 animate-pulse">
        <div className="h-6 w-40 bg-gray-200 rounded mb-2"></div>
        <div className="h-4 w-48 bg-gray-200 rounded mb-4"></div>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-20 bg-gray-100 rounded"></div>
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 border rounded-lg bg-red-50 text-red-700 flex items-center justify-between">
        <span>Unable to load top-hub signal.</span>
        <button
          onClick={() => refetch()}
          className="text-sm font-medium underline hover:text-red-900"
        >
         
