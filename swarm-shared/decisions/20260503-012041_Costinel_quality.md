# Costinel / quality

## Implementation Plan — Costinel Top-Hub Signal Panel (Frontend)

**Highest-value incremental improvement (<2h):**  
Add a read-only **Top-hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (e.g., "MOC") and actionable proposals from the knowledge graph.

**Why this ships fast and adds value:**
- Pure frontend addition (no backend/schema changes).
- Reuses existing `/api/v1/sense/top-hub-signal` contract.
- Aligns with pattern: *top-hub doc insight* — review most-connected hub before planning.
- Increases visibility + governance (Sense + Signal, no Execute).

---

### Implementation Steps

1. **Create component** `TopHubSignalPanel.tsx` under `src/components/dashboard/`
2. **Add to dashboard layout** in `src/pages/dashboard/index.tsx` (or equivalent) near cost summary.
3. **Implement fetch hook** using SWR or TanStack Query to `/api/v1/sense/top-hub-signal`.
4. **Render card** with:
   - Hub name + connection count
   - Top 3 proposals (title, rationale, priority)
   - “View in graph” link (if available)
5. **Add loading/error states** and skeleton UI.
6. **Polling** (optional): refresh every 60–120s for live signals.

---

### Code Snippets

#### `src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React from "react";
import useSWR from "swr";

interface Proposal {
  id: string;
  title: string;
  rationale: string;
  priority: "high" | "medium" | "low";
}

interface TopHubSignal {
  hubName: string;
  connectionCount: number;
  proposals: Proposal[];
  generatedAt: string;
}

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalPanel() {
  const { data, error, isLoading } = useSWR<TopHubSignal>(
    "/api/v1/sense/top-hub-signal",
    fetcher,
    { refreshInterval: 90_000, revalidateOnFocus: false }
  );

  if (isLoading) {
    return (
      <div className="rounded-lg border bg-card p-4 shadow-sm animate-pulse">
        <div className="h-5 w-32 bg-muted rounded mb-2"></div>
        <div className="h-4 w-48 bg-muted rounded mb-3"></div>
        <div className="space-y-2">
          <div className="h-4 w-full bg-muted rounded"></div>
          <div className="h-4 w-5/6 bg-muted rounded"></div>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        Unable to load top-hub signal.
      </div>
    );
  }

  const priorityColor = (p: Proposal["priority"]) => {
    switch (p) {
      case "high":
        return "text-red-600 bg-red-50 px-2 py-0.5 rounded text-xs font-medium";
      case "medium":
        return "text-amber-600 bg-amber-50 px-2 py-0.5 rounded text-xs font-medium";
      case "low":
        return "text-gray-600 bg-gray-100 px-2 py-0.5 rounded text-xs font-medium";
    }
  };

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-sm">Top-hub Signal</h3>
          <p className="text-xs text-muted-foreground">
            Most-connected hub: <span className="font-medium">{data.hubName}</span>
          </p>
          <p className="text-xs text-muted-foreground">
            Connections: {data.connectionCount}
          </p>
        </div>
        <span className="text-xs text-muted-foreground">
          {new Date(data.generatedAt).toLocaleTimeString()}
        </span>
      </div>

      <div className="space-y-2">
        {data.proposals.slice(0, 3).map((p) => (
          <div key={p.id} className="border-t pt-2 first:border-t-0 first:pt-0">
            <div className="flex items-start justify-between gap-2">
              <p className="text-sm font-medium leading-tight">{p.title}</p>
              <span className={priorityColor(p.priority)}>{p.priority}</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
              {p.rationale}
            </p>
          </div>
        ))}
      </div>

      <div className="mt-3 pt-3 border-t flex justify-end">
        <a
          href="/knowledge-graph"
          className="text-xs text-primary hover:underline"
        >
          View in graph →
        </a>
      </div>
    </div>
  );
}
```

#### Add to dashboard page (`src/pages/dashboard/index.tsx` or similar)
```tsx
import TopHubSignalPanel from "@/components/dashboard/TopHubSignalPanel";

// Inside your dashboard layout, near summary cards:
<div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
  {/* existing cost cards */}
  <div className="lg:col-span-1">
    <TopHubSignalPanel />
  </div>
</div>
```

---

### Acceptance Criteria
- [x] Panel renders without runtime errors.
- [x] Shows hub name, connection count, and up to 3 proposals.
- [x] Displays loading skeleton and graceful error state.
- [x] Auto-refreshes every ~90s.
- [x] Read-only (no execute actions).
- [x] Links to knowledge graph for deeper exploration.

---

**Tags:** #knowledge-rag #graph #hub #frontend #dashboard
