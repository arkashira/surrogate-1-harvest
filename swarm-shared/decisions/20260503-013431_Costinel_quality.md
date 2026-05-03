# Costinel / quality

**Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)**

**Scope & Constraints**  
- Frontend-only, read-only panel.  
- Must surface the most-connected hub (e.g., “MOC”) and actionable proposals from the knowledge graph.  
- Timebox: <2h.  
- Stack: React + TypeScript + Tailwind.  
- Must gracefully degrade when backend/graph API is missing (local fixture + typed contracts).  
- No runtime exceptions; resilient UI.

---

### 1) Artifacts to create/modify
- `src/types/knowledge.ts` — single source of truth for contracts.  
- `src/lib/knowledge.ts` — fetcher with resilient fallback and lightweight validation.  
- `src/components/TopHubSignalPanel.tsx` — self-contained panel with loading/error/empty states.  
- Mount point: `src/app/dashboard/page.tsx` (or equivalent) — embed as a card.  
- Optional: `src/app/api/knowledge/top-hub/route.ts` — thin server handler if backend exists.

---

### 2) Type contract (`src/types/knowledge.ts`)
```ts
export interface KnowledgeHub {
  slug: string;       // e.g. "MOC"
  label: string;      // e.g. "Multi-Org Cost governance"
  rank: number;       // connection score
  description: string;
  tags: string[];     // e.g. ["knowledge-rag","graph","hub"]
}

export interface ActionableProposal {
  id: string;
  title: string;
  summary: string;
  impact: "high" | "medium" | "low";
  effort: "low" | "medium" | "high";
  hubSlug: string;
  href?: string;     // optional deep link
}

export interface TopHubPayload {
  hub: KnowledgeHub;
  proposals: ActionableProposal[];
  generatedAt: string; // ISO
}
```

---

### 3) Fetcher + resilient fixture (`src/lib/knowledge.ts`)
```ts
import { TopHubPayload } from '@/types/knowledge';

const ENDPOINT = '/api/knowledge/top-hub';

const FIXTURE: TopHubPayload = {
  hub: {
    slug: 'MOC',
    label: 'Multi-Org Cost governance',
    rank: 97,
    description:
      'Most-connected hub for cross-account cost governance and policy propagation.',
    tags: ['knowledge-rag', 'graph', 'hub'],
  },
  proposals: [
    {
      id: 'p-001',
      title: 'Standardize tag enforcement across orgs',
      summary: 'Apply mandatory cost-center + owner tags to reduce untracked spend.',
      impact: 'high',
      effort: 'medium',
      hubSlug: 'MOC',
    },
    {
      id: 'p-002',
      title: 'RI coverage analysis for top 5 services',
      summary: 'Run RI recommender and present 1-year savings scenarios.',
      impact: 'high',
      effort: 'low',
      hubSlug: 'MOC',
    },
  ],
  generatedAt: new Date().toISOString(),
};

export async function fetchTopHub(): Promise<TopHubPayload> {
  try {
    const res = await fetch(ENDPOINT, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as TopHubPayload;
    // Lightweight runtime validation
    if (!json?.hub?.slug || !Array.isArray(json.proposals)) {
      throw new Error('Invalid payload shape');
    }
    return json;
  } catch (err) {
    // Graceful degradation: return fixture and log non-blockingly
    console.warn('Top-hub API unavailable, using fixture', err);
    return FIXTURE;
  }
}
```

---

### 4) Panel component (`src/components/TopHubSignalPanel.tsx`)
```tsx
'use client';

import { useEffect, useState } from 'react';
import { fetchTopHub } from '@/lib/knowledge';
import type { TopHubPayload } from '@/types/knowledge';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const impactColor = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-green-100 text-green-800',
} as const;

const effortBadge = {
  low: 'bg-green-100 text-green-800',
  medium: 'bg-amber-100 text-amber-800',
  high: 'bg-gray-100 text-gray-800',
} as const;

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub()
      .then((v) => {
        if (mounted) setData(v);
      })
      .catch((err) => {
        if (mounted) setError(err);
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
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Loading top-hub signals...
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            <div className="h-4 w-3/4 animate-pulse rounded bg-muted" />
            <div className="h-4 w-1/2 animate-pulse rounded bg-muted" />
          </div>
        </CardContent>
      </Card>
    );
  }

  if (error || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Unable to load signals
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground/70">
            Please try again later.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base font-semibold">
            Top-hub Signal
          </CardTitle>
          <Badge variant="outline" className="text-xs">
            {data.hub.rank}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="mb-4">
          <h3 className="text-lg font-medium">{data.hub.label}</h3>
          <p className="text-sm text-muted-foreground">{data.hub.description}</p>
          <div className="mt-2 flex flex-wrap gap-1">
            {data.hub.tags.map((t) => (
              <Badge key={t} variant="secondary" className="text-xs">
                {t}
              </Badge>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <h4 className="text-sm font-medium text-muted-foreground">
            Actionable proposals
          </h4>
          {data.proposals.length === 0 && (
            <p className="text-sm text-muted-foreground/70">
              No proposals available.
            </p>
          )}
          {data.proposals.map((p) => (
            <div
              key={p.id}
              className="rounded border p-3 text-sm transition-colors hover:bg-muted/50"
            >
              <div className="flex items-start justify-between gap-2">
                <p className="font-medium leading-tight">{p.title}</p>
              </div>
              <p className="mt
