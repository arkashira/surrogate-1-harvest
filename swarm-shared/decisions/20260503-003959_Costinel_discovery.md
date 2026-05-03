# Costinel / discovery

## Final Synthesis — Costinel “Top-Hub Signal” Card (read-only)

**Chosen approach**: Pure-frontend, zero-backend card that surfaces the most-connected hub + 3 contextual signals.  
**Timebox**: ≤2h.  
**Core pattern**: Sense + Signal (read-only; no execution).

---

### 1) High-value improvement (merged rationale)
- Add a persistent, read-only **Top-Hub Signal** card to the Costinel dashboard.
- Identify the most-connected hub from the knowledge-rag graph (by degree) and display:
  - Hub name and short description/type.
  - Exactly 3 contextual signals (anomalies, cost spikes, governance gaps) derived from linked docs.
- Use **local/cached data by default** (no new API contract) with graceful fallback to a static snapshot.  
- Optional lightweight fetch to existing `/api/knowledge-rag/top-hub` **only if already available**; never block render.
- Links to deeper graph view or related docs for human review (no execution actions).

Why this wins:
- Fastest to ship (static/cached-first), avoids backend changes and network waterfalls.
- Highest reliability (fallback always present) and clear read-only semantics.
- Most actionable (immediately shows “MOC” or equivalent hub + 3 signals for review).

---

### 2) Concrete implementation plan (frontend-only)

**Files to touch**:
- `src/components/cards/TopHubSignalCard.tsx` (new)
- `src/pages/Dashboard.tsx` (mount card)
- `src/lib/knowledgeRag.ts` (fetcher + cached/static loader)
- `src/types/knowledgeRag.ts` (types)
- `src/data/knowledgeRag/top-hub.json` (committed static snapshot — new)

**Ordered steps** (fast, non-blocking):

1. Add minimal types (5 min).
2. Commit a small static snapshot (`top-hub.json`) representing the most-connected hub and 3 signals (10–15 min).
3. Add lightweight loader: try existing `/api/knowledge-rag/top-hub` if available; otherwise load static snapshot; fallback to in-code constant (10 min).
4. Build read-only card (30–45 min):
   - Show hub name, description/type.
   - List 3 signals with titles + snippets.
   - Optional link to graph or related docs (anchor, no execution).
   - Accessible, responsive, skeleton while loading.
5. Mount card on dashboard in a prominent, non-blocking zone (10 min).
6. Verify no waterfalls, offline behavior, and graceful fallback (10–15 min).

---

### 3) Unified code snippets

#### `src/types/knowledgeRag.ts`
```ts
export interface HubSignal {
  title: string;
  snippet: string;
  docId?: string;
  ts?: string;
  href?: string;
}

export interface TopHubResponse {
  hub: string;
  hubType?: string;
  description?: string;
  signals: HubSignal[];
  updatedAt?: string;
}
```

#### `src/data/knowledgeRag/top-hub.json` (committed static snapshot)
```json
{
  "hub": "MOC",
  "hubType": "Operations",
  "description": "Mission Operations Center — central hub for cost governance playbooks and runbooks.",
  "signals": [
    {
      "title": "AWS cost spike in us-east-1",
      "snippet": "Detected 42% increase vs 7d avg; review idle EC2.",
      "docId": "aws-cost-spike-2026-04-25"
    },
    {
      "title": "Unattached EBS volume trend",
      "snippet": "12 unattached volumes across 3 accounts; potential savings ~$380/mo.",
      "docId": "ebs-unattached-2026-04-24"
    },
    {
      "title": "Tag compliance drift",
      "snippet": "Finance tag missing on 7% of resources; may affect chargeback.",
      "docId": "tag-compliance-2026-04-23"
    }
  ],
  "updatedAt": "2026-04-27T00:00:00Z"
}
```

#### `src/lib/knowledgeRag.ts`
```ts
import type { TopHubResponse } from '@/types/knowledgeRag';

// In-code constant fallback (last-resort)
const FALLBACK_TOP_HUB: TopHubResponse = {
  hub: 'MOC',
  hubType: 'Operations',
  description: 'Mission Operations Center — central hub for cost governance playbooks and runbooks.',
  signals: [
    { title: 'AWS cost spike in us-east-1', snippet: 'Detected 42% increase vs 7d avg; review idle EC2.' },
    { title: 'Unattached EBS volume trend', snippet: '12 unattached volumes across 3 accounts; potential savings ~$380/mo.' },
    { title: 'Tag compliance drift', snippet: 'Finance tag missing on 7% of resources; may affect chargeback.' },
  ],
};

async function loadStaticSnapshot(): Promise<TopHubResponse> {
  try {
    // Vite/CRA-style import; adapt to your bundler if needed
    const mod = await import('@/data/knowledgeRag/top-hub.json');
    return mod.default || mod;
  } catch {
    return FALLBACK_TOP_HUB;
  }
}

export async function fetchTopHub(signal?: AbortSignal): Promise<TopHubResponse> {
  // Fast path: use existing API if reachable (no contract changes required)
  try {
    const res = await fetch('/api/knowledge-rag/top-hub', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal,
      cache: 'no-store',
    });

    if (res.ok) {
      const json = (await res.json()) as TopHubResponse;
      if (json?.hub && Array.isArray(json.signals)) return json;
    }
  } catch {
    // ignore and fallback to static
  }

  // Preferred static path (no network dependency)
  return loadStaticSnapshot();
}
```

#### `src/components/cards/TopHubSignalCard.tsx`
```tsx
import { useEffect, useState } from 'react';
import { fetchTopHub, type TopHubResponse } from '@/lib/knowledgeRag';

export function TopHubSignalCard() {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchTopHub(controller.signal)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  const topHub = data ?? (window as any).FALLBACK_TOP_HUB;
  const signals = (topHub?.signals ?? []).slice(0, 3);

  if (loading && !data) {
    return (
      <div className="rounded-lg border bg-card p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        <div className="mt-3 space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-4 w-full animate-pulse rounded bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <article className="rounded-lg border bg-card p-4 shadow-sm" aria-label="Top Hub Signal">
      <header className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold text-foreground">Top Hub</h3>
          <p className="text-lg font-medium">{topHub.hub}</p>
          {topHub.hubType && (
            <p className="text-xs text-muted-foreground">{topHub.hubType}</p>
          )}
          {topHub.description && (
            <p className="mt-1 text-xs text-muted-foreground">{topHub.description}</p>
