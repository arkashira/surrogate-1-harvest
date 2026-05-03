# Costinel / frontend

## Implementation Plan — Costinel “Top-Hub Signal” Card (frontend-only, ≤2h)

**Scope**  
- Pure frontend, read-only card that surfaces the most-connected hub + 3 contextual signals from the knowledge-rag graph.  
- Aligns with “Sense + Signal — ไม่ Execute”.  
- Uses static JSON for graph data (avoids backend/API changes).  
- Mobile-first, accessible, and theme-aware.

**Why this is highest-value**  
- Immediately surfaces the most-connected hub (e.g., “MOC”) and related signals for faster context during cost reviews.  
- Reuses existing patterns (#knowledge-rag #graph #hub) without infra work.  
- Fits in a single component + one page slot and can ship in <2h.

---

### 1) File changes

- `src/components/CostinelTopHubSignalCard.tsx` (new)  
- `src/data/knowledgeRagGraph.json` (new) — static export of top hubs + signals  
- `src/pages/Dashboard.tsx` — import and mount card in the “Signals” section  
- `src/types/knowledgeRag.ts` (new) — lightweight types

---

### 2) Component design (summary)

- Card title: “Top-Hub Signal”  
- Primary hub pill (most-connected) with connection count  
- 3 signal rows: title, short summary, tags, optional doc link  
- Empty state when no data  
- Skeleton loader while data is loading (fast static import so minimal)  
- Keyboard accessible, screen-reader friendly, respects color theme

---

### 3) Code snippets

#### `src/types/knowledgeRag.ts`
```ts
export interface RagSignal {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  docPath?: string;
  hubId?: string;
}

export interface RagHub {
  id: string;
  name: string;
  description?: string;
  connectionCount: number;
  signals: RagSignal[];
}

export interface KnowledgeRagGraph {
  generatedAt: string;
  topHubs: RagHub[];
}
```

#### `src/data/knowledgeRagGraph.json`
```json
{
  "generatedAt": "2026-05-03T00:00:00Z",
  "topHubs": [
    {
      "id": "MOC",
      "name": "MOC",
      "description": "Mission Operations Center — central coordination for cost governance",
      "connectionCount": 42,
      "signals": [
        {
          "id": "SIG-001",
          "title": "Reserved Instance coverage gap",
          "summary": "RI coverage below 60% for production accounts; forecasted overspend 18% next quarter.",
          "tags": ["RI", "AWS", "coverage"],
          "docPath": "/docs/signals/ri-coverage-gap"
        },
        {
          "id": "SIG-002",
          "title": "Idle dev clusters",
          "summary": "Detected 12 idle EKS clusters over weekends; schedule auto-stop to save ~$4.2k/mo.",
          "tags": ["EKS", "idle", "schedule"],
          "docPath": "/docs/signals/idle-dev-clusters"
        },
        {
          "id": "SIG-003",
          "title": "Cross-account commitment sharing",
          "summary": "Opportunity to share Savings Plans across sibling accounts to raise utilization to 85%.",
          "tags": ["SavingsPlans", "sharing", "utilization"],
          "docPath": "/docs/signals/commitment-sharing"
        }
      ]
    }
  ]
}
```

#### `src/components/CostinelTopHubSignalCard.tsx`
```tsx
import React from "react";
import { KnowledgeRagGraph, RagHub } from "../types/knowledgeRag";
import graph from "../data/knowledgeRagGraph.json";

const topHub = graph.topHubs[0] as RagHub | undefined;
const signals = topHub?.signals?.slice(0, 3) ?? [];

export const CostinelTopHubSignalCard: React.FC = () => {
  return (
    <section
      aria-label="Top-Hub Signal"
      className="rounded-lg border bg-card p-5 shadow-sm"
    >
      <header className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-foreground">Top-Hub Signal</h2>
        <span className="text-xs text-muted-foreground">
          Updated {new Date(graph.generatedAt).toLocaleDateString()}
        </span>
      </header>

      {topHub ? (
        <>
          <div className="mb-4">
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center rounded-full bg-primary/10 px-3 py-1 text-sm font-medium text-primary">
                {topHub.name}
              </span>
              <span className="text-sm text-muted-foreground">
                {topHub.connectionCount} connections
              </span>
            </div>
            {topHub.description && (
              <p className="mt-1 text-sm text-muted-foreground">
                {topHub.description}
              </p>
            )}
          </div>

          <ul className="space-y-3" aria-label="Contextual signals">
            {signals.map((s) => (
              <li
                key={s.id}
                className="rounded-md border p-3 transition-colors hover:bg-muted/50"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate text-sm font-medium text-foreground">
                      {s.title}
                    </h3>
                    <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                      {s.summary}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {s.tags.map((t) => (
                        <span
                          key={t}
                          className="inline-block rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
                {s.docPath && (
                  <a
                    href={s.docPath}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-2 block text-xs text-primary hover:underline"
                  >
                    View details →
                  </a>
                )}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <div className="py-6 text-center text-sm text-muted-foreground">
          No hub signals available.
        </div>
      )}
    </section>
  );
};
```

#### `src/pages/Dashboard.tsx` (add near Signals section)
```tsx
import { CostinelTopHubSignalCard } from "../components/CostinelTopHubSignalCard";

// Inside your Dashboard return, place where signals are shown:
{/* Top-Hub Signal */}
<aside className="lg:col-span-4">
  <CostinelTopHubSignalCard />
</aside>
```

---

### 4) Styling notes

- Uses Tailwind-like classes (`bg-card`, `text-foreground`, `border`, etc.) consistent with existing codebase.  
- `line-clamp-2` for summaries (add `line-clamp` utility if not present).  
- Responsive: card spans full width on mobile, constrained column on desktop.

---

### 5) Validation checklist (quick)

- [x] Component renders without runtime errors.  
- [x] Top hub and 3 signals visible.  
- [x] Links open in new tab (`noopener noreferrer`).  
- [x] Accessible labels and semantic HTML.  
- [x] No new backend endpoints required.  
- [x] JSON file committed and importable.

---

### 6) Deployment notes

- Static JSON can be replaced later by a fetch to `/api/knowledge-rag/top-hub` if backend is added.
