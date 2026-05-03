# Costinel / frontend

I synthesize the strongest parts of both proposals (static data, zero-runtime, cost-impact focus, accessibility, and fast implementation) and resolve contradictions in favor of correctness + concrete actionability.

Final choice: **static import at build time** (not fetch) for guaranteed zero-runtime calls and simpler verification. I keep the richer payload shape from Candidate 2 and the pragmatic component structure from Candidate 1, with small, high-value additions (currency formatting, clear empty states, and a build-time verification step).

---

## Final Specification: Top-Hub Signal Panel (≤2h)

**Goal**  
Add a read-only, CDN-backed Top-Hub Signal Panel to the Costinel dashboard that surfaces the most-connected hub (default **MOC**) and its top 3 actionable, cost-aware signals.  
Rules:  
- Zero runtime API calls (no client fetch).  
- Strictly additive and non-mutating (Sense + Signal — ไม่ Execute).  
- Mobile-first, accessible, and build-verifiable.

---

## Implementation Plan (≤2h)

1. **Static data artifact** (10m)  
   Create `public/signals/top-hub-moc.json` (CDN-deployed with app).  
   - Includes `hub`, `title`, `summary`, `signals[]` with `costImpactUsd`.

2. **Type definitions** (10m)  
   Add `src/types/signals.ts` for strict interfaces and reuse.

3. **Panel component** (45m)  
   Add `src/components/TopHubSignalPanel.tsx`:  
   - Import JSON at build time (`import signals from '../public/signals/top-hub-moc.json'`).  
   - Render accessible card list with impact badge, formatted cost, description, and tags.  
   - Empty/fallback states included.

4. **Dashboard integration** (15m)  
   Mount `<TopHubSignalPanel />` in `src/pages/Dashboard.tsx` below the primary cost summary and above trends.

5. **Polish & accessibility** (10m)  
   - Use existing design tokens.  
   - Semantic headings, ARIA labels, focus-visible safe.  
   - Responsive grid (mobile-first).

6. **Build & verify** (10m)  
   - `npm run build` and confirm no runtime requests for signals.  
   - Smoke-test locally and commit.

---

## Code Snippets

### 1) Static signal payload (public/signals/top-hub-moc.json)

```json
{
  "hub": "MOC",
  "title": "Multi-Org Cost Guardrails",
  "summary": "Top-connected hub for cross-account cost governance. Focus on commitment coverage and anomaly containment.",
  "signals": [
    {
      "id": "moc-ri-coverage",
      "title": "RI/SP Coverage Gap",
      "description": "Compute spend >40% on on-demand across 3 linked accounts; recommend 12-month convertible RIs to cut cost ~22%.",
      "costImpactUsd": -48000,
      "tags": ["RI", "coverage", "compute", "high-impact"]
    },
    {
      "id": "moc-anomaly-detection",
      "title": "Anomaly: Nightly Dev Spike",
      "description": " EKS node group autoscale spikes 02:00–04:00 UTC; enforce schedule-based scaling policy to save ~$6k/mo.",
      "costImpactUsd": -18000,
      "tags": ["anomaly", "EKS", "autoscale", "medium-impact"]
    },
    {
      "id": "moc-snapshot-retention",
      "title": "Orphaned Snapshot Retention",
      "description": "500+ unattached volumes/snapshots >30 days; lifecycle policy to archive/delete frees 12TB and ~$1.2k/mo.",
      "costImpactUsd": -1200,
      "tags": ["storage", "snapshot", "retention", "low-effort"]
    }
  ]
}
```

---

### 2) Types (src/types/signals.ts)

```ts
export interface TopHubSignal {
  id: string;
  title: string;
  description: string;
  costImpactUsd: number; // negative = savings
  tags: string[];
}

export interface TopHubPayload {
  hub: string;
  title: string;
  summary: string;
  signals: TopHubSignal[];
}
```

---

### 3) Panel component (src/components/TopHubSignalPanel.tsx)

```tsx
import React from 'react';
import { TopHubPayload } from '../types/signals';
import topHubData from '../public/signals/top-hub-moc.json';

const data = topHubData as TopHubPayload;

function formatCurrency(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${value < 0 ? '-' : ''}$${(abs / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${value < 0 ? '-' : ''}$${(abs / 1_000).toFixed(0)}k`;
  return `${value < 0 ? '-' : ''}$${abs}`;
}

function impactFromCost(usd: number): 'high' | 'medium' | 'low' {
  const abs = Math.abs(usd);
  if (abs >= 20_000) return 'high';
  if (abs >= 5_000) return 'medium';
  return 'low';
}

const impactColors = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-green-100 text-green-800',
} as const;

const TopHubSignalPanel: React.FC = () => {
  if (!data?.signals?.length) {
    return (
      <section aria-label="Top-Hub Signals" className="mb-6">
        <p className="text-sm text-gray-400">No signals available.</p>
      </section>
    );
  }

  return (
    <section aria-label={`Top-Hub Signals — ${data.hub}`} className="mb-6">
      <header className="mb-3 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Top-Hub Signals</h2>
          <p className="text-xs text-gray-500">{data.summary}</p>
        </div>
        <span className="mt-1 inline-flex items-center rounded bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600 sm:mt-0">
          {data.hub}
        </span>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data.signals.map((signal) => {
          const impact = impactFromCost(signal.costImpactUsd);
          return (
            <article
              key={signal.id}
              className="rounded border bg-white p-4 shadow-sm"
            >
              <div className="mb-2 flex items-start justify-between gap-2">
                <h3 className="text-sm font-semibold text-gray-900">{signal.title}</h3>
                <span
                  className={`ml-2 shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${impactColors[impact]}`}
                >
                  {formatCurrency(signal.costImpactUsd)}/mo
                </span>
              </div>
              <p className="mb-3 text-xs text-gray-600">{signal.description}</p>
              <div className="flex flex-wrap gap-1" aria-label="Tags">
                {signal.tags.map((tag) => (
                  <span
                    key={tag}
                    className="rounded bg-gray-50 px-1.5 py-0.5 text-xs text-gray-500"
                  >
                    {tag}
                  </span>
                ))}
              </div>
