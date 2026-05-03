# Costinel / quality

## Final Implementation Plan — Top-hub Signal Panel (Costinel dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and up to 3 actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind — resilient to missing/partial graph data, CDN-first fetch, deterministic fallbacks, and accessible UI.

---

### Unified acceptance criteria
- [ ] Panel visible on dashboard (root or `/dashboard`) labeled **“Top-hub Signal”**.
- [ ] Shows the most-connected hub: name, optional description, connection count, and tags.
- [ ] Shows up to **3 actionable proposals** with title, rationale/summary, impact badge, and “Review” CTA.
- [ ] “Review” opens `href` in a new tab (or emits an event) and respects security (`noopener`).
- [ ] Deterministic loading, error, and empty states; no runtime exceptions.
- [ ] Graceful degradation: CDN fetch → `/api/knowledge/top-hub` → local fixture → safe default.
- [ ] Accessibility: semantic markup, ARIA states, focus-visible controls, keyboard-friendly.
- [ ] Responsive layout and Tailwind-consistent styling; no breaking changes.

---

### Unified data contract (canonical)

```ts
// src/types/knowledge-graph.ts
export type HubType = 'MOC' | 'Service' | 'Account' | 'Policy' | 'Other';

export interface HubNode {
  id: string;
  name: string;
  label?: string;
  type?: HubType;
  description?: string;
  connectionCount: number;
  tags?: string[];
}

export interface Proposal {
  id: string;
  title: string;
  rationale: string;
  summary?: string;
  impact?: 'High' | 'Medium' | 'Low';
  actionUrl?: string;
  actionLabel?: string; // default "Review"
  href?: string;        // alias for actionUrl
  tags?: string[];
}

export interface TopHubSignal {
  hub: HubNode | null;
  proposals: Proposal[];
  generatedAt?: string;
}
```

---

### Fetch strategy (CDN-first, resilient)

```ts
// src/lib/knowledge-graph.ts
import type { TopHubSignal } from '../types/knowledge-graph';

const CDN_URL =
  'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub-signal.json';
const API_URL = '/api/knowledge/top-hub';
const LOCAL_URL = '/mock/top-hub-signal.json';

async function tryFetch<T>(url: string): Promise<T | null> {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchTopHubSignal(): Promise<TopHubSignal> {
  return (
    (await tryFetch<TopHubSignal>(CDN_URL)) ??
    (await tryFetch<TopHubSignal>(API_URL)) ??
    (await tryFetch<TopHubSignal>(LOCAL_URL)) ??
    { hub: null, proposals: [] }
  );
}
```

---

### API adapter (optional backend route)

If you expose `/api/knowledge/top-hub`, return the same `TopHubSignal` shape.  
Example (Next.js-like):

```ts
// src/pages/api/knowledge/top-hub.ts  (or app/api/knowledge/top-hub/route.ts)
import type { NextApiRequest, NextApiResponse } from 'next';
import type { TopHubSignal } from '../../../types/knowledge-graph';

export default function handler(_: NextApiRequest, res: NextApiResponse<TopHubSignal>) {
  // Replace with real graph query; fallback to fixture in dev
  const payload: TopHubSignal = {
    hub: {
      id: 'hub-1',
      name: 'Identity & Access Management',
      type: 'Service',
      description: 'Central IAM for workforce and workloads.',
      connectionCount: 42,
      tags: ['IAM', 'Zero Trust', 'MFA']
    },
    proposals: [
      {
        id: 'p-1',
        title: 'Enforce phishing-resistant MFA for all privileged roles',
        rationale: 'Reduces credential compromise risk for high-privilege accounts.',
        impact: 'High',
        tags: ['MFA', 'Privileged Access']
      },
      {
        id: 'p-2',
        title: 'Consolidate SaaS app onboarding through IdP',
        rationale: 'Improves visibility and control over SaaS access.',
        impact: 'Medium',
        tags: ['SaaS', 'IdP']
      },
      {
        id: 'p-3',
        title: 'Automate quarterly access reviews for critical apps',
        rationale: 'Ensures least-privilege and timely revocation.',
        impact: 'Medium',
        tags: ['Access Reviews', 'Compliance']
      }
    ]
  };

  res.status(200).json(payload);
}
```

---

### Panel component (final)

```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState, useCallback } from 'react';
import { fetchTopHubSignal } from '../lib/knowledge-graph';
import type { TopHubSignal, HubNode, Proposal } from '../types/knowledge-graph';

const EMPTY: TopHubSignal = { hub: null, proposals: [] };

export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState<TopHubSignal>(EMPTY);
  const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('loading');

  const load = useCallback(async () => {
    setStatus('loading');
    try {
      const data = await fetchTopHubSignal();
      setSignal(data ?? EMPTY);
      setStatus('idle');
    } catch {
      setStatus('error');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (status === 'loading') {
    return (
      <section aria-busy="true" className="rounded-lg border bg-white p-4 shadow-sm">
        <p className="text-sm text-gray-500">Loading top-hub signal…</p>
      </section>
    );
  }

  if (status === 'error' || !signal) {
    return (
      <section className="rounded-lg border bg-white p-4 shadow-sm">
        <p className="text-sm text-gray-500">Unable to load top-hub signal.</p>
        <button
          onClick={load}
          className="mt-2 text-sm text-blue-600 underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
        >
          Retry
        </button>
      </section>
    );
  }

  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm" aria-label="Top-hub signal">
      <HubInfo hub={signal.hub} />
      <ProposalsList proposals={signal.proposals} />
    </section>
  );
}

function HubInfo({ hub }: { hub: HubNode | null }) {
  if (!hub) return null;
  return (
    <div className="mb-3">
      <h3 className="text-base font-semibold text-gray-900">{hub.name}</h3>
      {hub.description && <p className="mt-1 text-sm text-gray-600">{hub.description}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
        <span>{hub.connectionCount} connections</span>
        {hub.type && <span className="rounded bg-gray-100 px-1.5 py-0.5">{hub.type}</span>}
        {hub.tags && hub.tags.length > 0 && (
          <span className="flex flex-wrap gap-1">
            {hub.tags.map((t) => (
              <span key={t} className="rounded bg-gray-100 px-1.5 py-0.5">
                {t}
              </span>
            ))}
          </span>
        )}
      </div>

