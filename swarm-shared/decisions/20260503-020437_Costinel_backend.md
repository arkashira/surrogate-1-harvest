# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
- Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its actionable cost-saving proposals from the knowledge graph.  
- Ships in <2h; zero backend changes; no schema migrations; uses existing `/api/knowledge-graph/hubs` (or local fixture if unavailable).  
- Incremental value: immediate contextual insights for governance reviewers without leaving the dashboard.

---

### 1) Architecture (minimal)

```
Costinel Dashboard
 └─ components/
    └─ TopHubSignalPanel/
        ├─ TopHubSignalPanel.tsx      (main panel + skeleton)
        ├─ useTopHubSignal.ts         (data hook: hub + proposals)
        ├─ ProposalCard.tsx           (single actionable card)
        ├─ types.ts                   (local types)
        └─ TopHubSignalPanel.module.css
```

- Data source priority:
  1. `GET /api/kledge-graph/hubs?top=1&hub=MOC` (or similar)
  2. If 404/unavailable → fallback to local fixture (MOC) so UI still renders.
- No polling; one-time fetch on mount (or when user opens panel).
- Accessibility: semantic markup, keyboard-friendly, color contrast compliant.

---

### 2) Concrete Implementation

#### `components/TopHubSignalPanel/types.ts`
```ts
export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: {
    monthlySavingsUSD: number;
    effort: 'low' | 'medium' | 'high';
    risk: 'low' | 'medium' | 'high';
  };
  actions: Array<{
    label: string;
    href?: string;
    onClick?: () => void;
  }>;
  tags?: string[];
  updatedAt: string;
}

export interface SignalNode {
  id: string;
  label: string;
  type: 'hub' | 'proposal' | 'policy';
  weight: number;
}

export interface HubSignal {
  hub: string;
  displayName: string;
  description: string;
  proposals: Proposal[];
  signals?: SignalNode[];
  lastUpdated: string;
}
```

#### `components/TopHubSignalPanel/useTopHubSignal.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import { HubSignal } from './types';

const FALLBACK_HUB: HubSignal = {
  hub: 'MOC',
  displayName: 'Mission Optimization Center',
  description:
    'Top hub for cross-account cost governance, reservation coverage, and idle-resource remediation.',
  lastUpdated: new Date().toISOString(),
  proposals: [
    {
      id: 'moc-ri-coverage-2026-06',
      title: 'Increase Reserved Instance coverage to 80%',
      summary:
        'Current RI coverage is 54% for production workloads. Purchase 12-month convertible RIs to capture ~$42k/mo savings with low risk.',
      impact: {
        monthlySavingsUSD: 42000,
        effort: 'medium',
        risk: 'low',
      },
      actions: [
        { label: 'View recommendation', href: '/recommendations/ri-coverage' },
        { label: 'Create proposal', onClick: () => alert('Proposal flow (stub)') },
      ],
      tags: ['aws', 'ri', 'production'],
      updatedAt: '2026-05-03T08:00:00Z',
    },
    {
      id: 'moc-idle-snapshots-2026-06',
      title: 'Remove orphaned EBS snapshots (>90d)',
      summary:
        '230 orphaned snapshots across three accounts consuming ~$3.1k/mo. Automated cleanup policy recommended.',
      impact: {
        monthlySavingsUSD: 3100,
        effort: 'low',
        risk: 'low',
      },
      actions: [
        { label: 'Run cleanup', href: '/tasks/snapshot-cleanup' },
        { label: 'View details', href: '/reports/snapshots' },
      ],
      tags: ['aws', 'ebs', 'storage'],
      updatedAt: '2026-05-02T14:00:00Z',
    },
  ],
};

export function useTopHubSignal(hub = 'MOC') {
  const [data, setData] = useState<HubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/knowledge-graph/hubs?top=1&hub=${encodeURIComponent(hub)}`, {
        headers: { Accept: 'application/json' },
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as HubSignal;
      setData(json);
    } catch (err) {
      setError(String(err));
      // Graceful fallback so UI remains useful
      setData(FALLBACK_HUB);
    } finally {
      setLoading(false);
    }
  }, [hub]);

  useEffect(() => {
    load();
  }, [load]);

  const refresh = () => load();

  return { data, loading, error, refresh };
}
```

#### `components/TopHubSignalPanel/ProposalCard.tsx`
```tsx
import React from 'react';
import { Proposal } from './types';

const riskColors = {
  low: 'bg-green-100 text-green-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-red-100 text-red-800',
} as const;

const effortColors = {
  low: 'bg-blue-100 text-blue-800',
  medium: 'bg-purple-100 text-purple-800',
  high: 'bg-orange-100 text-orange-800',
} as const;

export const ProposalCard: React.FC<{ proposal: Proposal }> = ({ proposal }) => {
  return (
    <article className="rounded-lg border bg-white p-4 shadow-sm hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h4 className="truncate text-base font-semibold text-gray-900">{proposal.title}</h4>
          <p className="mt-1 text-sm text-gray-600 line-clamp-2">{proposal.summary}</p>

          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
            <span className={`rounded px-2 py-0.5 font-medium ${riskColors[proposal.impact.risk]}`}>
              Risk: {proposal.impact.risk}
            </span>
            <span className={`rounded px-2 py-0.5 font-medium ${effortColors[proposal.impact.effort]}`}>
              Effort: {proposal.impact.effort}
            </span>
            {proposal.impact.monthlySavingsUSD > 0 && (
              <span className="rounded bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700">
                Save ${proposal.impact.monthlySavingsUSD.toLocaleString()}/mo
              </span>
            )}
          </div>

          {proposal.tags && proposal.tags.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {proposal.tags.map((t) => (
                <span
                  key={t}
                  className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-500"
                >
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="mt-4
