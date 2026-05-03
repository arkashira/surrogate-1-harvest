# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card  
*(Frontend-only, ≤2h, read-only, graceful fallback)*

### Synthesis & Resolution of Contradictions
- **Framework**: Candidate 1 uses React/Next.js; Candidate 2 uses Vue.  
  **Resolution**: Keep implementation **framework-agnostic in design**; provide both React and Vue snippets so teams can pick the stack in use. Prefer React/Next.js as the primary example (matches Candidate 1’s route-based API mock), but include Vue file for Vue-based deployments.

- **Data source**: Candidate 1 proposes optional API route + mock; Candidate 2 proposes static JSON only.  
  **Resolution**: Ship a **local mock as the default** and a **lightweight API route (optional)** for environments that want to proxy/cached upstream data. Card must never break if endpoint is missing.

- **Signal count**: Candidate 1 shows 3 signals; Candidate 2 shows 3 signals.  
  **Resolution**: Hard limit to **3 signals** in UI (slice). Validate payload length.

- **Graceful fallback**: Both agree on “unavailable” state.  
  **Resolution**: Neutral, non-error UI with no console spam. In dev, prefer local mock silently.

- **Pattern alignment**: “Sense + Signal — no Execute” and “Review most-connected hub before planning tasks” retained.

---

### High-value improvement
Add a **Top-Hub Signal** card to the Costinel dashboard that converts graph centrality into cost-governance signals (Sense + Signal), surfacing the highest-degree hub and 3 actionable summaries with links.

---

### Implementation Steps (≤2h)

1. **Define contract & mock** (10 min)  
   Create `mocks/knowledge-rag-top-hub.json` (or `src/data/mockKnowledgeGraph.json` for Vue) with deterministic fixture.

2. **Add fetch service with graceful fallback** (15 min)  
   Implement `knowledgeRagService` (React) or composable (Vue) that:
   - Tries `/api/knowledge-rag/top-hub` (if available).
   - Falls back to local mock in dev.
   - Returns `null` on failure (no throws, no console spam).

3. **Create reusable card component** (60 min)  
   - Show hub label, degree, description.
   - List max 3 signals (title + summary + link).
   - Skeleton, empty/unavailable state, accessible markup, responsive.

4. **Add optional API route** (10 min, optional)  
   - Next.js route or equivalent that returns mock (or proxies/caches upstream).  
   - Not required for MVP.

5. **Wire into dashboard** (15 min)  
   - Place in Signals or Governance panel.
   - Add config/feature-flag toggle to disable without deploy.

6. **Polish & test graceful degradation** (10 min)  
   - Simulate network failure and missing endpoint.
   - Verify no console errors and neutral UI.

---

### Data Contract (single source of truth)

```json
{
  "generatedAt": "2026-05-03T12:00:00Z",
  "topHub": {
    "id": "MOC",
    "label": "MOC",
    "degree": 42,
    "description": "Master Operating Contract — central governance artifact for cloud cost approvals.",
    "tags": ["governance", "operations", "cost-center"]
  },
  "signals": [
    {
      "id": "s1",
      "title": "RI coverage gap for prod workloads",
      "summary": "MOC-linked resources show 38% RI coverage; estimated 22% savings available.",
      "href": "/governance/proposals/ri-coverage-gap"
    },
    {
      "id": "s2",
      "title": "Unattached EBS volumes in us-east-1",
      "summary": "3 unattached volumes (~$210/mo) tied to MOC-owned accounts.",
      "href": "/governance/proposals/unattached-ebs"
    },
    {
      "id": "s3",
      "title": "Idle dev clusters over weekends",
      "summary": "MOC policy suggests weekend shutdown; projected $1.2k/mo savings.",
      "href": "/governance/proposals/idle-dev-shutdown"
    }
  ]
}
```

---

### Code Snippets

#### React (Next.js) — Service
```ts
// services/knowledgeRagService.ts
export interface TopHubSignal {
  id: string;
  title: string;
  summary: string;
  href: string;
}

export interface TopHubPayload {
  generatedAt: string;
  topHub: {
    id: string;
    label: string;
    degree: number;
    description: string;
    tags?: string[];
  };
  signals: TopHubSignal[];
}

const ENDPOINT = '/api/knowledge-rag/top-hub';

export async function fetchTopHub(): Promise<TopHubPayload | null> {
  try {
    const res = await fetch(ENDPOINT, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as TopHubPayload;
    if (!data?.topHub?.id || !Array.isArray(data.signals)) throw new Error('Invalid payload');
    return data;
  } catch (err) {
    if (process.env.NODE_ENV === 'development') {
      try {
        const mod = await import('../mocks/knowledge-rag-top-hub.json');
        return (mod.default || mod) as TopHubPayload;
      } catch {
        // noop
      }
    }
    return null;
  }
}
```

#### React — Card Component
```tsx
// components/TopHubSignalCard.tsx
import { Suspense } from 'react';
import { fetchTopHub, type TopHubPayload } from '@/services/knowledgeRagService';

function Skeleton() {
  return (
    <div className="rounded-lg border bg-card p-4 animate-pulse">
      <div className="flex items-b justify-between mb-2">
        <div className="h-5 w-24 bg-muted rounded" />
        <div className="h-4 w-12 bg-muted rounded" />
      </div>
      <div className="h-4 w-3/4 bg-muted rounded mb-3" />
      <div className="space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-14 bg-muted rounded" />
        ))}
      </div>
    </div>
  );
}

async function TopHubSignalCardInner() {
  const data = await fetchTopHub();

  if (!data) {
    return (
      <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        Insights unavailable — knowledge graph not reachable.
      </div>
    );
  }

  const { topHub, signals } = data;

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="font-semibold text-base">Top Hub: {topHub.label}</h3>
        <span className="text-xs text-muted-foreground">degree {topHub.degree}</span>
      </div>
      <p className="text-sm text-muted-foreground mb-3">{topHub.description}</p>

      <div className="space-y-2">
        {signals.slice(0, 3).map((s) => (
          <a
            key={s.id}
            href={s.href}
            className="block p-2 rounded-md border bg-background hover:bg-accent transition-colors"
          >
            <div className="text-sm font-medium">{s.title}</div>
            <div className="text-xs text-muted-foreground mt-0.5">{s.summary}</div>
          </a>
        ))}
      </div>
    </div>
  );
}

export default function TopHubSignalCard() {
  return (
    <Suspense fallback={<Skeleton />}>
      <TopHubSignalCardInner />
    </Suspense>
  );
}
```

#### Vue (Alternative) — Component

