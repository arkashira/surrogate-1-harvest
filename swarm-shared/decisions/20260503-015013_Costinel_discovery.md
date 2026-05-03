# Costinel / discovery

## Final Implementation Plan  
*(Synthesized from Candidates 1 & 2 — strongest parts, resolved contradictions, maximally correct + actionable)*

**Scope & constraints**  
- Frontend-only, read-only panel.  
- Default hub: **MOC**.  
- Resilient to missing backend: robust local fallback.  
- SSR-friendly hydration + lightweight client-side refresh.  
- Zero new runtime dependencies beyond existing stack (React/TS + Tailwind).  
- Ship target: <2 hours.

---

### 1) File changes (relative to `/opt/axentx/Costinel`)

```
src/
 ├─ components/
 │   └─ TopHubSignalPanel.tsx        (new)
 ├─ hooks/
 │   └─ useTopHub.ts                 (new)
 ├─ pages/
 │   └─ Dashboard.tsx                (modify)
 ├─ lib/
 │   └─ topHubApi.ts                 (new)
 └─ config/
     └─ knowledgeGraph.ts            (new)
```

---

### 2) Static fallback config (Type-safe)

`src/config/knowledgeGraph.ts`
```ts
export type Impact = 'high' | 'medium' | 'low';
export type Priority = Impact; // alias used by adapter/UI

export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: Impact;
  priority: Priority;
  tags: string[];
  href?: string;
}

export interface HubNode {
  slug: string;
  label: string;
  description: string;
  proposals: Proposal[];
  updatedAt: string; // ISO
}

export const HUBS: Record<string, HubNode> = {
  MOC: {
    slug: 'MOC',
    label: 'Mission Operations Center',
    description:
      'Orchestration hub for cloud-cost governance workflows, policy-as-code, and cross-account guardrails.',
    updatedAt: '2026-05-03T01:47:52Z',
    proposals: [
      {
        id: 'MOC-001',
        title: 'Enforce tag-compliance on new resources via policy-as-code',
        summary:
          'Require mandatory tags (owner, cost-center, env) at creation time using OPA/Conftest and admission controllers.',
        impact: 'high',
        priority: 'high',
        tags: ['governance', 'tags', 'policy-as-code'],
        href: '/proposals/MOC-001',
      },
      {
        id: 'MOC-002',
        title: 'Right-size underutilized EKS node groups',
        summary:
          'Downsize node groups with <35% avg CPU over 14d; estimated savings 22% on compute.',
        impact: 'high',
        priority: 'high',
        tags: ['eks', 'rightsize', 'compute'],
        href: '/proposals/MOC-002',
      },
      {
        id: 'MOC-003',
        title: 'Convert steady-state RDS to reserved instances (1yr, partial upfront)',
        summary:
          'Covers 68% of steady-state DB workload; forecasted 37% cost reduction vs on-demand.',
        impact: 'medium',
        priority: 'medium',
        tags: ['rds', 'ri', 'forecast'],
        href: '/proposals/MOC-003',
      },
    ],
  },
} as const;

export const DEFAULT_HUB = 'MOC';
export type TopHubPayload = {
  hub: { name: string; description: string; slug: string };
  proposals: Proposal[];
};
```

---

### 3) API adapter with resilient fallback

`src/lib/topHubApi.ts`
```ts
import { DEFAULT_HUB, HUBS, type TopHubPayload } from '../config/knowledgeGraph';

const API_ENDPOINT = '/api/knowledge-graph/hubs/top';

export async function fetchTopHub(slug?: string): Promise<TopHubPayload> {
  const target = slug || DEFAULT_HUB;

  try {
    const res = await fetch(`${API_ENDPOINT}/${encodeURIComponent(target)}`, {
      // If credentials/cookies needed, add credentials: 'include'
      cache: 'no-store',
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as TopHubPayload;
    // Basic shape validation (lightweight)
    if (!data?.hub?.name || !Array.isArray(data.proposals)) throw new Error('Invalid payload');
    return data;
  } catch {
    // CDN-friendly, zero-runtime-dependency fallback
    const hub = HUBS[target];
    if (!hub) throw new Error(`Hub "${target}" not found in fallback`);
    return {
      hub: { name: hub.label, description: hub.description, slug: hub.slug },
      proposals: hub.proposals,
    };
  }
}
```

---

### 4) Hook: SSR-friendly + client refresh

`src/hooks/useTopHub.ts`
```ts
import { useEffect, useState } from 'react';
import { fetchTopHub, type TopHubPayload } from '../lib/topHubApi';

export function useTopHub(initialData?: TopHubPayload, slug?: string) {
  const [data, setData] = useState<TopHubPayload | undefined>(initialData);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const payload = await fetchTopHub(slug);
      setData(payload);
      setError(null);
    } catch (err: any) {
      setError(err?.message || 'Failed to load hub data');
    } finally {
      setLoading(false);
    }
  };

  // initial hydration: do not refetch if we already have initialData
  useEffect(() => {
    if (!initialData) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // optional background refresh (lightweight)
  useEffect(() => {
    if (!data) return;
    const id = setTimeout(() => {
      // silent refresh every ~60s
      fetchTopHub(slug).then((p) => setData(p)).catch(() => {});
    }, 60_000);
    return () => clearTimeout(id);
  }, [data, slug]);

  return { data, loading, error, refetch: load };
}
```

---

### 5) Panel component (SSR + client)

`src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useTopHub } from '../hooks/useTopHub';
import type { Proposal, TopHubPayload } from '../config/knowledgeGraph';

const impactColors = {
  high: 'bg-red-50 border-red-200 text-red-800',
  medium: 'bg-amber-50 border-amber-200 text-amber-800',
  low: 'bg-emerald-50 border-emerald-200 text-emerald-800',
};

export function TopHubSignalPanel({
  initialData,
  hubSlug,
}: {
  initialData?: TopHubPayload;
  hubSlug?: string;
}) {
  const { data, loading, error } = useTopHub(initialData, hubSlug);

  if (loading) {
    return (
      <section className="rounded-xl border border-gray-200 bg-white p-5">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 animate-pulse rounded-lg bg-gray-200" />
          <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        </div>
        <div className="mt-4 space-y-3">
          {[...Array(2)].map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg bg-gray-50" />
          ))}
        </div>
      </section>
    );
  }

  if (error || !data) {

