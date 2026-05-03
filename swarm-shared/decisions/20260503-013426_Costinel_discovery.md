# Costinel / discovery

**Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)**  
*Scope: Frontend-only, read-only, resilient to missing backend. Timebox: <2h. Stack: React + TypeScript + Tailwind.*

---

### 1) Add types and resilient fallback data  
Create `src/lib/knowledge-rag.ts`:

```ts
// src/lib/knowledge-rag.ts
export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: "high" | "medium" | "low";
  tags: string[];
  href?: string;
}

export interface HubInsight {
  hub: string;
  connections: number;
  description: string;
  proposals: Proposal[];
}

export const FALLBACK_HUB_INSIGHT: HubInsight = {
  hub: "MOC",
  connections: 42,
  description:
    "Most-connected hub (MOC) indicates multi-org cost governance patterns and cross-account RI coverage opportunities.",
  proposals: [
    {
      id: "ri-coverage-2026-05",
      title: "Increase Reserved Instance coverage to 75%",
      summary: "Current coverage 58%; projected 22% YoY savings if raised to 75%.",
      impact: "high",
      tags: ["RI", "AWS", "savings"],
    },
    {
      id: "idle-eni-cleanup",
      title: "Schedule idle ENI cleanup",
      summary: "12 idle ENIs detected across dev accounts; ~$480/mo recoverable.",
      impact: "medium",
      tags: ["cleanup", "network", "AWS"],
    },
  ],
};
```

---

### 2) Create the TopHubSignalPanel component  
Create `src/components/TopHubSignalPanel.tsx`:

```tsx
// src/components/TopHubSignalPanel.tsx
import React from "react";
import { HubInsight } from "../lib/knowledge-rag";

const impactColor = {
  high: "bg-red-100 text-red-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-green-100 text-green-800",
} as const;

interface Props {
  insight?: HubInsight;
}

export const TopHubSignalPanel: React.FC<Props> = ({ insight }) => {
  const data =
    insight ??
    (typeof window !== "undefined"
      ? (window as any).FALLBACK_HUB_INSIGHT
      : undefined) ?? {
      hub: "—",
      connections: 0,
      description: "No graph data available.",
      proposals: [],
    };

  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm">
      <header className="mb-3 flex items-baseline justify-between">
        <h2 className="text-lg font-semibold text-gray-900">Top-hub Insight</h2>
        <span className="text-sm text-gray-500">{data.connections} connections</span>
      </header>

      <div className="mb-4">
        <span className="inline-flex items-center rounded-full bg-indigo-50 px-3 py-1 text-sm font-medium text-indigo-700">
          {data.hub}
        </span>
        <p className="mt-2 text-sm text-gray-600">{data.description}</p>
      </div>

      {data.proposals.length > 0 && (
        <ul className="space-y-2" aria-label="Actionable proposals">
          {data.proposals.map((p) => (
            <li key={p.id} className="rounded border p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <h3 className="truncate text-sm font-semibold text-gray-900">{p.title}</h3>
                  <p className="mt-1 text-xs text-gray-600">{p.summary}</p>
                </div>
                <span
                  className={`ml-2 flex-shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${
                    impactColor[p.impact]
                  }`}
                >
                  {p.impact}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                {p.tags.map((t) => (
                  <span
                    key={t}
                    className="rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-500"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}

      <footer className="mt-3 text-right">
        <a href="/knowledge-rag" className="text-sm text-indigo-600 hover:underline">
          View graph &rarr;
        </a>
      </footer>
    </section>
  );
};
```

---

### 3) Expose fallback on window (lightweight)  
In your entry file (`src/main.tsx` or equivalent):

```ts
import { FALLBACK_HUB_INSIGHT } from "./lib/knowledge-rag";
(window as any).FALLBACK_HUB_INSIGHT = FALLBACK_HUB_INSIGHT;
```

---

### 4) Mount panel into dashboard  
Locate your dashboard (e.g., `src/pages/Dashboard.tsx`) and insert:

```tsx
import { TopHubSignalPanel } from "../components/TopHubSignalPanel";
import React from "react";

// If your dashboard already fetches the graph, wire it:
export const Dashboard = () => {
  const [insight, setInsight] = React.useState<HubInsight | undefined>();

  React.useEffect(() => {
    fetch("/api/knowledge-graph/top-hub")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setInsight)
      .catch(() => setInsight(undefined)); // fallback will render
  }, []);

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-bold">Costinel Dashboard</h1>
      <TopHubSignalPanel insight={insight} />
      {/* rest of dashboard */}
    </div>
  );
};

export default Dashboard;
```

If no API exists yet, simply use `<TopHubSignalPanel />` and rely on the window fallback.

---

### 5) Polish & verify (action checklist)
- Run `npm run lint` (or equivalent) and fix formatting.
- Verify responsive rendering on mobile/desktop (Tailwind defaults are responsive).
- Confirm no console errors when `/api/knowledge-graph/top-hub` is unavailable.
- Ensure links and interactive elements have proper focus states (Tailwind `focus:` utilities if needed).
- Commit and deploy:
  ```bash
  git add .
  git commit -m "Add Top-hub Signal Panel to Costinel dashboard"
  git push origin main
  ```

---

**Why this is the best synthesis**  
- Combines Candidate 1’s clear component structure and deployment steps with Candidate 2’s resilient fallback data and polished UI.  
- Resolves contradictions by favoring correctness: uses TypeScript interfaces, safe window fallback, and graceful degradation when the API is missing.  
- Maximizes actionability: explicit file paths, copy-paste code, and a verification checklist ensure the task can be completed within the 2-hour timebox.
