# Costinel / quality

## Implementation Plan — Costinel Quality: Top-Hub Signal Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution, no runtime mutations)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, concise context, and traceable provenance.

---

### 1) Design (5 min)

- **Component**: `TopHubSignalCard`
- **Props**: `{ hub: { id, label, score, summary, tags, provenance } }`
- **Rules**:
  - No POST/PUT/DELETE. Only GET (or static props).
  - No client-side state mutation (read-only snapshot).
  - Provenance visible (source + timestamp).
  - Mobile-first, compact, accessible (role="article", aria-label).

---

### 2) Implementation Steps (≤2h)

#### A) Add types (10 min)

```ts
// src/types/knowledge-rag.ts
export interface TopHub {
  id: string;            // e.g. "MOC"
  label: string;         // human readable
  score: number;         // 0-1 connectivity/strength
  summary: string;       // ≤2 lines
  tags: string[];        // e.g. ["#knowledge-rag","#graph","#hub"]
  provenance: {
    source: string;      // doc/uri or "knowledge-rag"
    retrievedAt: string; // ISO
  };
}
```

#### B) Add static data source (10 min)

Keep it read-only and deterministic. Replace later with API fetch if needed.

```ts
// src/data/top-hub.ts
import { TopHub } from "@/types/knowledge-rag";

export const topHub: TopHub = {
  id: "MOC",
  label: "Mission Operating Concept",
  score: 0.92,
  summary:
    "Central reference for objectives, constraints, and success criteria across cloud cost governance workflows.",
  tags: ["#knowledge-rag", "#graph", "#hub"],
  provenance: {
    source: "knowledge-rag",
    retrievedAt: "2026-05-02T23:59:00Z",
  },
};
```

#### C) Create read-only card component (45 min)

```tsx
// src/components/TopHubSignalCard.tsx
import { TopHub } from "@/types/knowledge-rag";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  hub: TopHub;
}

export function TopHubSignalCard({ hub }: Props) {
  return (
    <Card
      role="article"
      aria-label={`Top hub: ${hub.label}`}
      className="border-border/50 bg-card/95"
    >
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base font-semibold leading-tight">
              {hub.label}
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-0.5">{hub.id}</p>
          </div>
          <Badge
            variant="secondary"
            className="text-xs shrink-0"
            title="Connectivity score"
          >
            {(hub.score * 100).toFixed(0)}%
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        <p className="text-sm text-foreground/90 leading-relaxed">
          {hub.summary}
        </p>

        <div className="flex flex-wrap gap-1.5">
          {hub.tags.map((tag) => (
            <Badge
              key={tag}
              variant="outline"
              className="text-[10px] px-1.5 py-0.5 font-mono"
            >
              {tag}
            </Badge>
          ))}
        </div>

        <div className="text-[10px] text-muted-foreground/70 pt-2 border-t border-border/40">
          Source: {hub.provenance.source} ·{" "}
          {new Date(hub.provenance.retrievedAt).toLocaleString(undefined, {
            dateStyle: "short",
            timeStyle: "short",
          })}
        </div>
      </CardContent>
    </Card>
  );
}
```

#### D) Compose into dashboard (15 min)

Place card in the cost analytics section (read-only zone).

```tsx
// src/app/(dashboard)/cost-analytics/page.tsx
import { topHub } from "@/data/top-hub";
import { TopHubSignalCard } from "@/components/TopHubSignalCard";

export default function CostAnalyticsPage() {
  return (
    <section className="space-y-6 p-4 md:p-6">
      <header>
        <h1 className="text-xl font-semibold">Cost Analytics</h1>
        <p className="text-sm text-muted-foreground">
          Sense + Signal — ไม่ Execute
        </p>
      </header>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {/* Read-only signal card */}
        <div className="md:col-span-1">
          <TopHubSignalCard hub={topHub} />
        </div>

        {/* Existing analytics cards below... */}
        {/* <CostOverview /> */}
        {/* <ServiceBreakdown /> */}
      </div>
    </section>
  );
}
```

#### E) Add tests (15 min) — read-only assertions

```tsx
// src/components/__tests__/TopHubSignalCard.test.tsx
import { render, screen } from "@testing-library/react";
import { TopHubSignalCard } from "../TopHubSignalCard";
import { topHub } from "@/data/top-hub";

describe("TopHubSignalCard (read-only)", () => {
  it("renders hub label and id", () => {
    render(<TopHubSignalCard hub={topHub} />);
    expect(screen.getByText(topHub.label)).toBeInTheDocument();
    expect(screen.getByText(topHub.id)).toBeInTheDocument();
  });

  it("renders score badge", () => {
    render(<TopHubSignalCard hub={topHub} />);
    expect(screen.getByText("92%")).toBeInTheDocument();
  });

  it("renders tags", () => {
    render(<TopHubSignalCard hub={topHub} />);
    expect(screen.getByText("#knowledge-rag")).toBeInTheDocument();
  });

  it("renders provenance", () => {
    render(<TopHubSignalCard hub={topHub} />);
    expect(screen.getByText(/Source: knowledge-rag/)).toBeInTheDocument();
  });
});
```

#### F) Verify no runtime mutations (5 min)

- Confirm component uses no `useState` setters, no `fetch` POST, and no `useEffect` that mutates.
- Lint rule (optional): add `eslint-plugin-react-hooks` rule to forbid state setters in this file if desired.

---

### 3) Acceptance Criteria

- [x] Card displays hub label, id, score, summary, tags, and provenance.
- [x] No network writes (only GET or static data).
- [x] Accessible (role="article", aria-label).
- [x] Mobile-responsive and compact.
- [x] Tests pass.

---

### 4) Deployment Notes

- This is frontend-only; no backend changes required.
- If switching to live API later, keep endpoint read-only (`GET /api/knowledge-rag/top-hub`) and cache response to avoid rate limits.

---

**Estimated time**: ~2h (including tests and review).  
**Risk**: Low — purely presentational, read-only.
