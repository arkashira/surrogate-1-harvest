# Costinel / quality

## Implementation Plan — Top-hub Signal Panel (Costinel dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind (resilient to missing backend; graceful fallback UI).  
**API contract**:
- `GET /api/knowledge-graph/top-hub` → `{ hub: { key: string; label: string; type: string; connections: number; lastUpdated: string }; proposals: Array<{ id: string; title: string; description: string; impact: "high"|"medium"|"low"; effort: "low"|"medium"|"high"; tags: string[] }> }`
- CDN/static fallback: load `/data/top-hub.json` if API unavailable.

---

### 1) Add API route (Next.js API route or proxy endpoint)

`/pages/api/knowledge-graph/top-hub.ts`

```ts
import type { NextApiRequest, NextApiResponse } from 'next';

export type TopHubResponse = {
  hub: {
    key: string;
    label: string;
    type: string;
    connections: number;
    lastUpdated: string;
  };
  proposals: Array<{
    id: string;
    title: string;
    description: string;
    impact: 'high' | 'medium' | 'low';
    effort: 'low' | 'medium' | 'high';
    tags: string[];
  }>;
};

// Lightweight in-memory cache to avoid repeated upstream calls during dev/preview
let cached: TopHubResponse | null = null;
let cachedAt = 0;
const TTL = 60_000; // 1m

export default async function handler(req: NextApiRequest, res: NextApiResponse<TopHubResponse>) {
  if (cached && Date.now() - cachedAt < TTL) {
    return res.status(200).json(cached);
  }

  try {
    // Replace with real graph service call. For now, return deterministic sample tied to MOC (most-connected hub).
    const result: TopHubResponse = {
      hub: {
        key: 'MOC',
        label: 'MOC (Management of Change)',
        type: 'process',
        connections: 124,
        lastUpdated: new Date().toISOString(),
      },
      proposals: [
        {
          id: 'prop-001',
          title: 'Standardize change-request tagging for cloud resources',
          description: 'Enforce mandatory cost-center and owner tags on MOC-triggered resource changes to improve attribution and anomaly detection.',
          impact: 'high',
          effort: 'medium',
          tags: ['governance', 'tagging', 'cost-visibility'],
        },
        {
          id: 'prop-002',
          title: 'Add pre-approval budget gates for high-impact MOC proposals',
          description: 'Require budget guardrails for changes estimated to increase monthly spend >10%.',
          impact: 'high',
          effort: 'low',
          tags: ['governance', 'budget-guardrails'],
        },
        {
          id: 'prop-003',
          title: 'Link MOC records to cost anomaly tickets',
          description: 'Auto-correlate recent MOC events with cost anomalies to accelerate root-cause analysis.',
          impact: 'medium',
          effort: 'medium',
          tags: ['automation', 'anomaly-correlation'],
        },
      ],
    };

    cached = result;
    cachedAt = Date.now();
    res.status(200).json(result);
  } catch (err) {
    // Fallback to static file to keep panel available
    try {
      // In a real Next.js app you'd import or read from public/data/top-hub.json
      const fallback = {
        hub: { key: 'MOC', label: 'MOC (Management of Change)', type: 'process', connections: 0, lastUpdated: new Date().toISOString() },
        proposals: [],
      };
      res.status(200).json(fallback);
    } catch (e) {
      res.status(503).json({ hub: { key: '', label: '', type: '', connections: 0, lastUpdated: '' }, proposals: [] });
    }
  }
}
```

---

### 2) Create reusable UI component

`/components/TopHubSignalPanel.tsx`

```tsx
import { useEffect, useState } from 'react';

type Hub = {
  key: string;
  label: string;
  type: string;
  connections: number;
  lastUpdated: string;
};

type Proposal = {
  id: string;
  title: string;
  description: string;
  impact: 'high' | 'medium' | 'low';
  effort: 'low' | 'medium' | 'high';
  tags: string[];
};

type TopHubResponse = {
  hub: Hub;
  proposals: Proposal[];
};

const impactColor = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-green-100 text-green-800',
} as const;

const effortColor = {
  low: 'bg-green-50 text-green-700',
  medium: 'bg-blue-50 text-blue-700',
  high: 'bg-purple-50 text-purple-700',
} as const;

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    fetch('/api/knowledge-graph/top-hub', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('API unavailable');
        return r.json();
      })
      .then((json) => {
        if (mounted) {
          setData(json);
          setError(false);
        }
      })
      .catch(() => {
        // Try CDN/static fallback
        fetch('/data/top-hub.json', { cache: 'no-store' })
          .then((r) => r.json())
          .then((json) => {
            if (mounted) {
              setData(json);
              setError(false);
            }
          })
          .catch(() => {
            if (mounted) setError(true);
          })
          .finally(() => {
            if (mounted) setLoading(false);
          });
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 animate-pulse rounded-lg bg-gray-200" />
          <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        </div>
        <div className="mt-4 space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg bg-gray-50" />
          ))}
        </div>
      </section>
    );
  }

  if (error || !data) {
    return (
      <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <p className="text-sm text-gray-500">Unable to load top-hub signals. Please try again later.</p>
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
      {/* Hub header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex
