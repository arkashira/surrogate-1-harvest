# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: ≤2h (frontend-only, no backend changes).  
**Principle**: “Sense + Signal — No Execute.” Read-only, static-first, extensible to live graph API later with zero UI churn.

---

### Why this is the highest-value incremental improvement
- Applies validated **top-hub doc insight** and **knowledge-rag** patterns already in the project.
- Delivers immediate governance context (Sense + Signal) without touching production infra or risking execution paths.
- Static local JSON ships in <2h; later replaceable by live graph queries behind an API with no UI changes.

---

### Implementation Steps (≤2h)

1. **Add seed data file** (5m)  
   Create `src/data/top-hub.json` with `hub`, `signals[]`, `updatedAt`, and `source`.

2. **Create card component** (45m)  
   - `TopHubSignalCard` (React + Tailwind) that loads the JSON (typed import).  
   - Renders hub name, short description, connection count, and 3 signals with icons and severity styling.  
   - Fully read-only; no mutations or API calls.  
   - Accessible (contrast, focus states, `aria-label`, keyboard navigation) and responsive (desktop 3-col signals, mobile stacked).

3. **Place card on dashboard** (20m)  
   - Mount in the dashboard sidebar or top-row widget area (example: `src/pages/Dashboard.tsx`).  
   - Keep card compact; allow future expandable behavior for full context.

4. **Add tests and docs** (20m)  
   - Snapshot test for card rendering.  
   - Small README note for ops on how to update the JSON (format, versioning, `updatedAt`).

5. **Verify** (10m)  
   - Smoke test on localhost: render, no console errors, mobile viewport OK.

---

### Code Snippets

#### 1) Seed data (src/data/top-hub.json)
```json
{
  "hub": {
    "id": "MOC",
    "name": "MOC",
    "description": "Most-connected hub in the knowledge-rag graph; central to cost governance decisions and cross-cloud policy signals.",
    "connectedCount": 127
  },
  "signals": [
    {
      "id": "s1",
      "title": "RI Coverage Gap",
      "summary": "AWS production accounts show 38% RI coverage; opportunity to shift 22% of on-demand spend to 1-year convertible RIs.",
      "severity": "medium",
      "icon": "ChartBarIcon"
    },
    {
      "id": "s2",
      "title": "Orphaned Volumes Spike",
      "summary": "Detected 14 unattached gp3 volumes across dev accounts (~$210/mo). Recommend snapshot + delete workflow.",
      "severity": "high",
      "icon": "ExclamationCircleIcon"
    },
    {
      "id": "s3",
      "title": "Commit-Cap Pressure",
      "summary": "HF ingestion repo nearing 128 commits/hr cap. Enable sibling repo sharding (hash-slug routing) to raise aggregate throughput.",
      "severity": "low",
      "icon": "InformationCircleIcon"
    }
  ],
  "updatedAt": "2025-11-01T12:00:00Z",
  "source": "knowledge-rag graph (read-only)"
}
```

#### 2) TopHubSignalCard component (src/components/TopHubSignalCard.tsx)
```tsx
import React from "react";
import topHubData from "../data/top-hub.json";

type Severity = "high" | "medium" | "low";

const severityColors: Record<Severity, string> = {
  high: "border-red-200 bg-red-50 text-red-800",
  medium: "border-amber-200 bg-amber-50 text-amber-800",
  low: "border-blue-200 bg-blue-50 text-blue-800",
};

const iconMap: Record<string, React.ComponentType<React.SVGProps<SVGSVGElement>>> = {
  ChartBarIcon: (props) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" {...props}>
      <path d="M12 20V10M18 20V4M6 20v-6" />
    </svg>
  ),
  ExclamationCircleIcon: (props) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 8v4M12 16h.01" />
    </svg>
  ),
  InformationCircleIcon: (props) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
  ),
};

export const TopHubSignalCard: React.FC = () => {
  const { hub, signals, updatedAt, source } = topHubData;

  return (
    <section
      className="rounded-xl border bg-white p-5 shadow-sm transition-shadow hover:shadow-md"
      aria-label={`Top hub: ${hub.name} — ${hub.description}`}
    >
      <div className="mb-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">{hub.name}</h2>
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
            {hub.connectedCount} connections
          </span>
        </div>
        <p className="mt-1 text-sm text-gray-600">{hub.description}</p>
      </div>

      <ul className="space-y-3" role="list">
        {signals.map((s) => {
          const Icon = iconMap[s.icon] ?? iconMap.InformationCircleIcon;
          return (
            <li
              key={s.id}
              className={`rounded-lg border p-3 ${severityColors[s.severity]}`}
            >
              <div className="flex gap-3">
                <div className="flex-shrink-0">
                  <Icon className="h-5 w-5 flex-shrink-0" aria-hidden="true" />
                </div>
                <div className="min-w-0">
                  <p className="text-sm font-medium">{s.title}</p>
                  <p className="mt-0.5 text-xs">{s.summary}</p>
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      <footer className="mt-4 flex items-center justify-between text-xs text-gray-400">
        <span>Updated: {new Date(updatedAt).toLocaleDateString()}</span>
        <span>{source}</span>
      </footer>
    </section>
  );
};
```

#### 3) Mount in dashboard (example: src/pages/Dashboard.tsx)
```tsx
import { TopHubSignalCard } from "../components/TopHubSignalCard";

export default function Dashboard() {
  return (
    <main className="mx-auto max-w-7xl p-4 md:p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Cost Governance Dashboard</h1>
      </div>

      {/* Top row: KPI cards */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">

