# Costinel / quality

**Final implementation plan — Top-hub Signal Panel (Costinel dashboard)**

**Scope**  
Frontend-only, read-only panel that surfaces the most-connected hub and actionable proposals from the knowledge graph. Timeboxed to <2h. Resilient to missing backend: uses static fallback, no layout shift, zero new runtime dependencies, accessible markup, and typed contracts.

---

### 1) Types (single source)

`src/types/knowledge-graph.ts`

```ts
export interface KnowledgeHub {
  id: string;
  label: string;
  description?: string;
  connections: number;
  lastUpdated?: string; // ISO
}

export interface SignalProposal {
  id: string;
  title: string;
  reason?: string;     // why this was surfaced
  summary?: string;    // short human summary
  hubId?: string;
  action?: string;     // human action hint (non-executing)
  impactUsd?: number;
  confidence?: number; // 0–1
  priority?: 'high' | 'medium' | 'low';
  tags?: string[];
  evidenceLinks?: Array<{ label: string; href: string }>;
}

export interface TopHubPayload {
  hub: KnowledgeHub | null;
  proposals: SignalProposal[];
  generatedAt: string;
}
```

---

### 2) API adapter (graceful, no-auth, CDN-ready)

`src/lib/knowledge-api.ts`

```ts
import type { TopHubPayload } from '../types/knowledge-graph';

const ENDPOINT = '/api/knowledge/top-hub';

const FALLBACK: TopHubPayload = {
  hub: {
    id: 'MOC',
    label: 'MOC',
    description: 'Most-connected operational hub',
    connections: 142,
    lastUpdated: new Date().toISOString(),
  },
  proposals: [
    {
      id: 'prop-001',
      title: 'Shift non-prod RIs to convertible',
      reason: 'High forecast variance in dev accounts',
      impactUsd: 18400,
      confidence: 0.82,
      priority: 'high',
      tags: ['RI', 'Coverage', 'Dev'],
    },
  ],
  generatedAt: new Date().toISOString(),
};

export async function fetchTopHub(signal?: AbortSignal): Promise<TopHubPayload> {
  try {
    const res = await fetch(ENDPOINT, { cache: 'no-store', signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as TopHubPayload;
    // Basic shape validation (lightweight)
    if (!json || typeof json !== 'object') throw new Error('Invalid payload shape');
    return {
      ...json,
      hub: json.hub || FALLBACK.hub,
      proposals: Array.isArray(json.proposals) ? json.proposals : FALLBACK.proposals,
    };
  } catch {
    // Graceful fallback: keep UI usable
    return FALLBACK;
  }
}
```

---

### 3) Hook (polling + accessibility-friendly loading states)

`src/hooks/useTopHub.ts`

```ts
import { useEffect, useState, useCallback } from 'react';
import type { TopHubPayload } from '../types/knowledge-graph';
import { fetchTopHub } from '../lib/knowledge-api';

export function useTopHub(pollIntervalMs = 60000) {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const payload = await fetchTopHub(signal);
      setData(payload);
      setError(null);
    } catch (err: any) {
      setError(err?.message || 'Failed to load top-hub signal');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);

    const id = setInterval(() => load(controller.signal), pollIntervalMs);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, [load, pollIntervalMs]);

  return { data, loading, error, refetch: () => load() };
}
```

---

### 4) Component (no layout shift, accessible, Tailwind)

`src/components/TopHubSignalPanel.tsx`

```tsx
import React from 'react';
import { useTopHub } from '../hooks/useTopHub';
import type { KnowledgeHub, SignalProposal } from '../types/knowledge-graph';

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/10">
      {children}
    </span>
  );
}

function ProposalRow({ p }: { p: SignalProposal }) {
  const impact = typeof p.impactUsd === 'number' ? `$${(p.impactUsd / 1000).toFixed(0)}k` : null;
  const confidence = typeof p.confidence === 'number' ? `${Math.round(p.confidence * 100)}% conf` : null;

  return (
    <article className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-gray-900">{p.title}</p>
        <p className="truncate text-xs text-gray-500">{p.reason || p.summary || '—'}</p>
        {p.tags && p.tags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1" aria-hidden="false">
            {p.tags.map((t) => (
              <Pill key={t}>{t}</Pill>
            ))}
          </div>
        )}
      </div>
      <div className="flex-shrink-0 text-right">
        {impact && <p className="text-sm font-semibold text-gray-900">{impact}</p>}
        {confidence && <p className="text-xs text-gray-400">{confidence}</p>}
      </div>
    </article>
  );
}

function HubCard({ hub }: { hub: KnowledgeHub }) {
  return (
    <div className="mb-4 flex items-center gap-3 rounded-lg bg-slate-50 p-3">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-indigo-50 text-indigo-600" aria-hidden="true">
        <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
        </svg>
      </div>
      <div>
        <p className="text-sm font-semibold text-gray-900">{hub.label}</p>
        <p className="text-xs text-gray-500">
          {hub.connections} connections — {hub.description || '—'}
        </p>
      </div>
    </div>
  );
}

export default function TopHubSignalPanel() {
  const { data, loading, error } = useTopHub();
  const hub = data?.hub ?? null;
  const proposals = data?.proposals ?? [];

  return (
    <section
      className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm"
      aria-busy={loading}
      aria-live="polite"
    >
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text
