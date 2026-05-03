# Costinel / backend

## Final Synthesis — Costinel “Top-Hub Signal” Card (≤2h, frontend-only)

### Non-negotiable constraints
- **Pure frontend** — zero backend, zero new APIs, no auth/infra changes.  
- **Read-only** — Sense + Signal only.  
- **Timeboxed ≤2h** — minimal, high-value UI.  
- **Reuse existing patterns** — design tokens, routing, data layer, and build tooling in `/opt/axentx/Costinel`.  
- **Safe to embed** — no side effects in dashboard.

---

### Highest-value incremental improvement
Add a **Top-Hub Signal Card** to the dashboard that surfaces the most-connected knowledge-rag hub (e.g., “MOC”) with contextual insights and quick links to related docs. This applies `knowledge-rag`, `graph`, and `hub` patterns and gives immediate governance context without backend work.

---

### Concrete implementation steps

1. **Locate dashboard layout**  
   Find the main card grid/container (likely `src/components/dashboard/DashboardGrid.tsx` or `src/pages/Dashboard.tsx`). Identify where to insert the new card (top row, first column for high visibility).

2. **Create TopHubSignalCard component**  
   - File: `src/components/cards/TopHubSignalCard.tsx`.  
   - Use existing design tokens (colors, spacing, typography).  
   - Accept props for hub data (shape below) and graph link.  
   - Static seed data for now (replaceable later via props/context).  
   - Renders:
     - Hub label + type badge.
     - Short insight text.
     - Connection count + optional signal chips.
     - List of related doc links (truncated).
     - “View in Knowledge Graph” action (opens existing graph viewer with node preselected).

3. **Integrate into dashboard**  
   Import and place `TopHubSignalCard` in the first column or top row of the card grid. Ensure it doesn’t break layout or introduce side effects.

4. **Styling & polish**  
   - Reuse existing card styles (Tailwind or CSS modules).  
   - Responsive (mobile-first).  
   - Simple hover states.  
   - Use existing icon set or lightweight icon (e.g., `GitBranchIcon` or `GraphIcon`).

5. **Verify & commit**  
   Run dev server, confirm card renders and doesn’t break layout. Commit with clear message.

---

### Hub node shape (static seed)
```ts
interface RelatedDoc {
  title: string;
  href: string;
  badge?: string;
}

interface HubNode {
  id: string;
  label: string;
  type: string;
  connections: number;
  signals?: string[];
  links: RelatedDoc[];
}
```

---

### Code snippets

#### `src/components/cards/TopHubSignalCard.tsx`
```tsx
import React from 'react';
import { Link } from 'react-router-dom';

interface RelatedDoc {
  title: string;
  href: string;
  badge?: string;
}

interface HubNode {
  id: string;
  label: string;
  type: string;
  connections: number;
  signals?: string[];
  links: RelatedDoc[];
}

interface TopHubSignalCardProps {
  hubNode?: HubNode;
  insight?: string;
  graphHref?: string;
}

const defaultHubNode: HubNode = {
  id: 'moc',
  label: 'MOC',
  type: 'Governance',
  connections: 42,
  signals: ['High impact', 'Policy exception'],
  links: [
    { title: 'Cost policy exceptions', href: '/docs/policy-exceptions' },
    { title: 'Owner registry', href: '/docs/owner-registry' },
    { title: 'Change management playbook', href: '/docs/change-playbook', badge: 'New' },
  ],
};

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  hubNode = defaultHubNode,
  insight = 'Most-connected governance hub — central to cost policy exceptions and owner mapping. Review before approving high-impact proposals.',
  graphHref = '/knowledge-graph',
}) => {
  return (
    <div className="rounded-lg border bg-card p-5 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <svg
            className="h-5 w-5 text-primary"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M13 10V3L4 14h7v7l9-11h-7z"
            />
          </svg>
          <h3 className="font-semibold text-foreground">Top-Hub Signal</h3>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className="inline-flex items-center rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
            {hubNode.label}
          </span>
          <span className="text-xs text-muted-foreground">{hubNode.type}</span>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
        <span>{hubNode.connections} connections</span>
        {hubNode.signals?.map((signal) => (
          <span
            key={signal}
            className="rounded bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary"
          >
            {signal}
          </span>
        ))}
      </div>

      <p className="mt-3 text-sm text-muted-foreground leading-relaxed">{insight}</p>

      <ul className="mt-4 space-y-2" aria-label="Related documents">
        {hubNode.links.map((doc) => (
          <li key={doc.href}>
            <Link
              to={doc.href}
              className="flex items-center gap-2 text-sm text-primary hover:underline focus:outline-none focus:underline"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-primary/60" aria-hidden="true" />
              <span>{doc.title}</span>
              {doc.badge && (
                <span className="ml-auto rounded bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary">
                  {doc.badge}
                </span>
              )}
            </Link>
          </li>
        ))}
      </ul>

      <div className="mt-4">
        <Link
          to={`${graphHref}?node=${hubNode.id}`}
          className="inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline focus:outline-none focus:underline"
        >
          View in Knowledge Graph
          <svg
            className="h-4 w-4"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </Link>
      </div>
    </div>
  );
};
```

#### Integrate into dashboard (example)
```tsx
// src/pages/Dashboard.tsx (or wherever the card grid lives)
import { TopHubSignalCard } from '@/components/cards/TopHubSignalCard';

export default function Dashboard() {
  return (
    <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
      {/* First column, top placement for high visibility */}
      <div className="md:col-span-1">
        <TopHubSignalCard />
      </div>

      {/* Existing cards follow */}
      {/* <CostSummaryCard /> */}
      {/* <AnomaliesCard /> */}
      {/* ... */}
    </div>
  );
}
```

---
