# Costinel / discovery

## Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the highest-connected hub and its actionable proposals. Resilient to missing backend with graceful fallback UI.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind

---

### 1) Unified types and API contract

Create `src/types/knowledge-rag.ts`:

```ts
export interface KnowledgeHub {
  id: string;
  label: string;
  type: 'hub' | 'proposal' | 'doc';
  connections: number;
  lastUpdated: string;
  tags: string[];
}

export interface SignalProposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  effort: 'low' | 'medium' | 'high';
  roi?: string;
  hubId: string;
  actions: Array<{
    label: string;
    href?: string;
    onClick?: () => void;
  }>;
  evidenceRefs: string[];
}

export interface RelatedDoc {
  id: string;
  title: string;
  source: string;
  snippet: string;
  href?: string;
}

export interface KnowledgeSignal {
  hub: KnowledgeHub;
  proposals: SignalProposal[];
  relatedDocs: RelatedDoc[];
}

export interface KnowledgeRagResponse {
  signals: KnowledgeSignal[];
  meta: {
    generatedAt: string;
    source: 'knowledge-rag';
  };
}
```

---

### 2) Resilient hook with polling, fast-fail, and fallback

Create `src/hooks/useTopHubSignal.ts`:

```ts
import { useEffect, useState, useCallback } from 'react';
import type { KnowledgeSignal } from '../types/knowledge-rag';

const ENDPOINT = '/api/knowledge-rag/signals';
const FALLBACK_SIGNAL: KnowledgeSignal = {
  hub: {
    id: 'MOC',
    label: 'MOC',
    type: 'hub',
    connections: 128,
    lastUpdated: new Date().toISOString(),
    tags: ['cost-governance', 'change-management', 'approval-workflow'],
  },
  proposals: [
    {
      id: 'p1',
      title: 'Standardize MOC approval SLA to 48h for cost-impacting changes',
      summary: 'Reduce cost-drift risk by enforcing faster review cycles for changes >$1k/mo.',
      impact: 'high',
      effort: 'medium',
      roi: '~12% reduction in unapproved spend',
      hubId: 'MOC',
      evidenceRefs: ['policy-v3.2', 'cost-drift-analysis-q3'],
      actions: [
        { label: 'View policy', href: '/policies/moc-sla' },
        { label: 'Create proposal', href: '/proposals/new?template=moc-sla' },
      ],
    },
  ],
  relatedDocs: [
    {
      id: 'd1',
      title: 'MOC Process v3.2',
      source: 'internal-wiki',
      snippet: 'Covers approval workflows, roles, and exception handling for cost-impacting changes.',
      href: '/docs/moc-process',
    },
  ],
};

export function useTopHubSignal(pollIntervalMs = 60000, enabled = true) {
  const [data, setData] = useState<KnowledgeSignal | null>(null);
  const [loading, setLoading] = useState<boolean>(!!enabled);
  const [error, setError] = useState<Error | null>(null);

  const fetchSignal = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    try {
      setLoading(true);
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 4000);

      const res = await fetch(ENDPOINT, {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        signal: controller.signal,
      });
      clearTimeout(timeout);

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: { signals: KnowledgeSignal[] } = await res.json();
      setData(json.signals?.[0] ?? FALLBACK_SIGNAL);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Unknown error'));
      setData(FALLBACK_SIGNAL);
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    fetchSignal();

    if (!enabled || pollIntervalMs <= 0) return;
    const id = setInterval(fetchSignal, pollIntervalMs);
    return () => clearInterval(id);
  }, [fetchSignal, pollIntervalMs, enabled]);

  const mutate = useCallback(async () => {
    await fetchSignal();
  }, [fetchSignal]);

  return { data, loading, error, mutate };
}
```

---

### 3) Top-hub Signal Panel component

Create `src/components/TopHubSignalPanel.tsx`:

```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import type { KnowledgeSignal } from '../types/knowledge-rag';

function ImpactBadge({ impact }: { impact: KnowledgeSignal['proposals'][0]['impact'] }) {
  const colors = {
    high: 'bg-red-100 text-red-800',
    medium: 'bg-amber-100 text-amber-800',
    low: 'bg-green-100 text-green-800',
  };
  return <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colors[impact]}`}>{impact}</span>;
}

function EffortBadge({ effort }: { effort: KnowledgeSignal['proposals'][0]['effort'] }) {
  const colors = {
    low: 'bg-green-100 text-green-800',
    medium: 'bg-blue-100 text-blue-800',
    high: 'bg-purple-100 text-purple-800',
  };
  return <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colors[effort]}`}>{effort}</span>;
}

export default function TopHubSignalPanel() {
  const { data, loading, error } = useTopHubSignal(60000, true);

  if (loading) {
    return (
      <div className="p-4 border border-gray-200 rounded-lg bg-gray-50">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-gray-200 animate-pulse" />
          <div className="space-y-2 flex-1">
            <div className="h-4 bg-gray-200 rounded w-1/3 animate-pulse" />
            <div className="h-3 bg-gray-200 rounded w-1/2 animate-pulse" />
          </div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="p-4 border border-gray-200 rounded-lg bg-white">
        <p className="text-sm text-gray-500">No signals available.</p>
      </div>
    );
  }

  const hub = data.hub;
  const primaryProposal = data.proposals[0];

  return (
    <div className="p-4 border border-gray-200 rounded-lg bg-white shadow-sm">
      {/* Header */}
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-cyan-100 flex items-center justify-center">
            <svg className="w-5 h-5 text-cyan-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
             
