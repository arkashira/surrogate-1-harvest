# Costinel / quality

## Implementation Plan — Costinel Quality: Top-Hub Signal Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution, no runtime mutations)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, concise context, and provenance — zero backend changes.

---

### 1) Highest-value incremental improvement
Add a **Top-Hub Signal Card** to the dashboard that:
- Shows the highest-centrality hub (title, score, short insight)
- Uses static/embedded hub graph snapshot (JSON) to avoid runtime API/write
- Links to related docs (anchors) for human review
- Renders in <100ms, zero client-side mutations, CSP-safe

---

### 2) Concrete implementation steps (frontend-only)

1. Create `src/data/hub-graph.json` (committed snapshot)
   - Deterministic top-hub selection (highest degree/pagerank)
   - Minimal shape: `{ hub, score, insight, relatedDocs, generatedAt }`

2. Add `src/components/TopHubSignalCard.tsx`
   - Pure presentational component
   - Accepts `hubGraph` prop (typed)
   - No state/effects that mutate; no fetch/POST

3. Wire into dashboard
   - Import snapshot and pass as prop
   - If dashboard is dynamic, load via static import (bundled) — no runtime fetch

4. Styling & accessibility
   - Use existing design tokens
   - `role="region" aria-label="Top hub signal"`

5. Build/test
   - Verify no network requests in devtools (no runtime API)
   - Lighthouse accessibility + performance checks

---

### 3) Code snippets

#### `src/data/hub-graph.json`
```json
{
  "hub": "MOC",
  "score": 0.92,
  "insight": "Most-connected governance node; central to cost policy propagation and anomaly triage.",
  "relatedDocs": [
    { "title": "Cost Policy Framework", "anchor": "#cost-policy" },
    { "title": "Anomaly Triage Playbook", "anchor": "#triage" }
  ],
  "generatedAt": "2026-05-02T23:59:00Z"
}
```

#### `src/components/TopHubSignalCard.tsx`
```tsx
import React from "react";

export interface RelatedDoc {
  title: string;
  anchor: string;
}

export interface HubGraph {
  hub: string;
  score: number;
  insight: string;
  relatedDocs: RelatedDoc[];
  generatedAt: string;
}

export interface TopHubSignalCardProps {
  hubGraph: HubGraph;
}

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({ hubGraph }) => {
  const { hub, score, insight, relatedDocs, generatedAt } = hubGraph;

  return (
    <section
      role="region"
      aria-label="Top hub signal"
      className="rounded-lg border bg-card p-4 shadow-sm"
    >
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-muted-foreground">Top Hub Signal</h3>
        <time dateTime={generatedAt} className="text-xs text-muted-foreground">
          {new Date(generatedAt).toLocaleDateString()}
        </time>
      </div>

      <div className="mt-2">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold">{hub}</span>
          <span className="text-sm text-muted-foreground">centrality</span>
        </div>
        <div className="mt-1 flex items-center gap-2">
          <div className="h-2 w-20 rounded-full bg-muted">
            <div
              className="h-2 rounded-full bg-primary transition-all"
              style={{ width: `${Math.min(100, Math.max(0, score * 100))}%` }}
            />
          </div>
          <span className="text-xs tabular-nums text-muted-foreground">
            {score.toFixed(2)}
          </span>
        </div>
      </div>

      <p className="mt-2 text-sm text-foreground">{insight}</p>

      {relatedDocs.length > 0 && (
        <ul className="mt-3 space-y-1">
          {relatedDocs.map((doc, idx) => (
            <li key={idx}>
              <a
                href={doc.anchor}
                className="text-xs text-primary hover:underline focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
              >
                {doc.title}
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};
```

#### Dashboard usage (example)
```tsx
import { TopHubSignalCard } from "@/components/TopHubSignalCard";
import hubGraph from "@/data/hub-graph.json";

export default function Dashboard() {
  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      <TopHubSignalCard hubGraph={hubGraph} />
      {/* other cards */}
    </div>
  );
}
```

---

### 4) Acceptance criteria (read-only)
- [ ] Card renders with hub, score, insight, and related doc links
- [ ] No runtime network requests for hub data (snapshot bundled)
- [ ] No client-side state mutations or POSTs
- [ ] Accessible (label, focusable links)
- [ ] Build passes; Lighthouse performance impact minimal

Ship this in <2h and keep Costinel strictly “Sense + Signal — ไม่ Execute.”
