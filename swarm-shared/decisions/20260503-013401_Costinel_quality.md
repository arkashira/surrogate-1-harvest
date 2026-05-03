# Costinel / quality

## Final Implementation Plan — Top-hub Signal Panel (Costinel dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub (e.g., "MOC") and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind. Resilient to missing graph data with CDN/local fallback and graceful UI.  
**Entry point**: `src/components/dashboard/TopHubSignalPanel.tsx` + register in dashboard layout.

---

### Why this is highest-value (<2h)
- Applies **top-hub doc insight** (#knowledge-rag #graph #hub) directly to Costinel’s governance surface.
- Read-only, no backend/infra changes — safe to ship.
- Improves decision velocity by surfacing the most-connected hub + proposals without leaving the dashboard.

---

### 1) Component contract (props + behavior)
- Props:
  - `hub?: Hub`
  - `loading?: boolean`
  - `onProposalClick?(proposalId: string): void`
  - `onRetry?(): void`
- Behavior:
  - `loading` → skeleton.
  - no `hub` → empty state with CTA to run knowledge-rag (docs link) + optional retry.
  - `hub` present → hub card + top 3 proposals (expandable list).
  - All strings escaped; no XSS risk.
  - Keyboard accessible and focus-visible.

---

### 2) Types (minimal, resilient)

#### `src/types/knowledgeGraph.ts`
```ts
export interface KnowledgeProposal {
  id: string;
  title: string;
  summary: string;
  impact: string;
  effort: string;
  tags: string[];
}

export interface KnowledgeHub {
  id: string;
  label: string;
  type: string;
  score: number;
  proposals: KnowledgeProposal[];
}

export interface KnowledgeGraphResponse {
  topHub?: KnowledgeHub;
  generatedAt?: string;
  source?: string;
}
```

---

### 3) Lightweight fetcher with CDN/local fallback

#### `src/api/knowledgeGraph.ts`
```ts
import type { KnowledgeGraphResponse } from '../types/knowledgeGraph';

const GRAPH_ENDPOINT = '/api/knowledge-graph/top-hub';
const CDN_FALLBACK = '/data/fallback-top-hub.json';

async function fetchGraph(): Promise<KnowledgeGraphResponse | null> {
  try {
    const res = await fetch(GRAPH_ENDPOINT, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Graph endpoint ${res.status}`);
    const json = (await res.json()) as KnowledgeGraphResponse;
    if (!json?.topHub) throw new Error('Missing topHub');
    return json;
  } catch {
    try {
      const res = await fetch(CDN_FALLBACK, { cache: 'max-age=300' });
      if (!res.ok) throw new Error('CDN fallback unavailable');
      return (await res.json()) as KnowledgeGraphResponse;
    } catch {
      return null;
    }
  }
}

export const knowledgeGraphApi = {
  fetchGraph,
};
```

---

### 4) Component implementation

#### `src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { Star, FileText, ChevronRight, AlertCircle, RefreshCw } from 'lucide-react';
import type { KnowledgeHub, KnowledgeProposal } from '../../types/knowledgeGraph';

interface TopHubSignalPanelProps {
  hub?: KnowledgeHub;
  loading?: boolean;
  onProposalClick?: (proposalId: string) => void;
  onRetry?: () => void;
}

const Pill = ({ children }: { children: React.ReactNode }) => (
  <span className="inline-flex items-center rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/10">
    {children}
  </span>
);

const ProposalRow: React.FC<{ proposal: KnowledgeProposal; onClick?: (id: string) => void }> = ({
  proposal,
  onClick,
}) => (
  <button
    type="button"
    onClick={() => onClick?.(proposal.id)}
    className="group flex w-full flex-col gap-1 rounded-lg border border-gray-100 bg-white p-3 text-left shadow-xs transition hover:border-gray-200 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
  >
    <div className="flex items-start justify-between gap-2">
      <div className="flex items-center gap-2">
        <FileText className="mt-0.5 size-4 shrink-0 text-gray-400 group-hover:text-gray-600" />
        <span className="truncate text-sm font-medium text-gray-900 group-hover:text-gray-900">
          {proposal.title}
        </span>
      </div>
      <ChevronRight className="mt-0.5 size-4 shrink-0 text-gray-300 transition group-hover:text-gray-400" />
    </div>
    <p className="text-xs text-gray-500 line-clamp-2">{proposal.summary}</p>
    <div className="mt-2 flex flex-wrap gap-1">
      <Pill>{proposal.impact}</Pill>
      <Pill>{proposal.effort}</Pill>
      {proposal.tags.slice(0, 2).map((t) => (
        <Pill key={t}>{t}</Pill>
      ))}
    </div>
  </button>
);

export const TopHubSignalPanel: React.FC<TopHubSignalPanelProps> = ({
  hub,
  loading,
  onProposalClick,
  onRetry,
}) => {
  if (loading) {
    return (
      <div className="rounded-xl border border-gray-100 bg-white p-5 shadow-sm" role="status">
        <div className="mb-4 h-6 w-32 animate-pulse rounded bg-gray-100" />
        <div className="space-y-3">
          <div className="h-10 animate-pulse rounded bg-gray-50" />
          <div className="h-10 animate-pulse rounded bg-gray-50" />
          <div className="h-10 animate-pulse rounded bg-gray-50" />
        </div>
      </div>
    );
  }

  if (!hub) {
    return (
      <div className="rounded-xl border border-gray-100 bg-white p-5 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-amber-50">
            <AlertCircle className="size-5 text-amber-500" />
          </div>
          <div className="flex-1">
            <h3 className="text-sm font-semibold text-gray-900">No hub signal available</h3>
            <p className="mt-1 text-xs text-gray-500">
              Run knowledge-rag to generate contextual insights from the graph. See docs for guidance.
            </p>
            <div className="mt-3 flex items-center gap-3">
              <a
                href="/docs/knowledge-rag"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline"
              >
                View knowledge-rag docs <ChevronRight className="size-3" />
              </a>
              {onRetry && (
                <button
                  type="button"
                  onClick={onRetry}
                  className="inline-flex items-center gap-1 text-xs font-medium text-gray-600 hover:text-gray-900"
                >
                  <RefreshCw className="size-3" />
                  Retry
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const topProposals = hub.proposals.slice(0, 3);

  return (
    <div className="rounded-xl border border-gray-100 bg
