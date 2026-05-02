# Costinel / quality

## Implementation Plan — Costinel Quality: Top-Hub Signal Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, rationale, and contextual signals for governance review.

---

### 1) Architecture (minimal, zero-write)

- **Data source**: static JSON produced by knowledge-rag pipeline (`/opt/axentx/Costinel/data/top-hub.json`)
  - Updated out-of-band (not by this UI)
- **UI**: React + Tailwind card in the quality dashboard
- **No API calls from frontend** — eliminates auth/rate-limit surface and keeps “no execute” invariant
- **No state mutations** — pure presentational component

---

### 2) File layout (existing paths assumed)

```
/opt/axentx/Costinel/
├── public/
│   └── data/
│       └── top-hub.json        <-- served statically (no auth)
├── src/
│   ├── components/
│   │   └── quality/
│   │       └── TopHubSignalCard.tsx
│   └── pages/
│       └── QualityDashboard.tsx
└── package.json
```

---

### 3) Static data contract (`public/data/top-hub.json`)

```json
{
  "hub": "MOC",
  "score": 0.92,
  "rationale": "Highest betweenness centrality across cost governance signals; primary mediation for RI, tagging, and anomaly workflows.",
  "signals": [
    { "type": "coverage", "label": "RI coverage", "value": "78%", "status": "ok" },
    { "type": "anomaly", "label": "Unusual spend", "value": "2 spikes", "status": "warn" },
    { "type": "tagging", "label": "Untagged resources", "value": "147", "status": "info" }
  ],
  "updatedAt": "2026-05-02T20:14:00Z"
}
```

---

### 4) Component implementation (`src/components/quality/TopHubSignalCard.tsx`)

```tsx
import React, { useEffect, useState } from "react";

type Signal = {
  type: "coverage" | "anomaly" | "tagging";
  label: string;
  value: string;
  status: "ok" | "warn" | "info";
};

type TopHubData = {
  hub: string;
  score: number;
  rationale: string;
  signals: Signal[];
  updatedAt: string;
};

const statusColors = {
  ok: "bg-green-100 text-green-800",
  warn: "bg-amber-100 text-amber-800",
  info: "bg-blue-100 text-blue-800",
};

const TopHubSignalCard: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Static, no-auth fetch from public path
    fetch("/data/top-hub.json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load top-hub data");
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-3 h-4 w-24 animate-pulse rounded bg-gray-200" />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5 text-sm text-gray-500 shadow-sm">
        Top hub signal unavailable
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-medium text-gray-500">Top Hub Signal</h3>
          <p className="mt-1 text-2xl font-semibold text-gray-900">{data.hub}</p>
        </div>
        <span className="inline-flex items-center rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-medium text-indigo-700">
          Score {(data.score * 100).toFixed(0)}%
        </span>
      </div>

      <p className="mt-3 text-sm text-gray-600">{data.rationale}</p>

      <dl className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        {data.signals.map((s, i) => (
          <div key={i} className="flex items-center gap-2 rounded-md border border-gray-100 bg-gray-50 px-3 py-2">
            <dt className="text-xs text-gray-500">{s.label}</dt>
            <dd className="ml-auto text-sm font-medium text-gray-900">{s.value}</dd>
            <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${statusColors[s.status]}`}>
              {s.status}
            </span>
          </div>
        ))}
      </dl>

      <p className="mt-3 text-xs text-gray-400">
        Updated {new Date(data.updatedAt).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}
      </p>
    </div>
  );
};

export default TopHubSignalCard;
```

---

### 5) Mount in dashboard (`src/pages/QualityDashboard.tsx`)

```tsx
import React from "react";
import TopHubSignalCard from "../components/quality/TopHubSignalCard";

const QualityDashboard: React.FC = () => {
  return (
    <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <h1 className="text-xl font-semibold text-gray-900">Quality Governance</h1>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <TopHubSignalCard />
        {/* other quality cards */}
      </div>
    </main>
  );
};

export default QualityDashboard;
```

---

### 6) Build & verification checklist (≤2h)

- [ ] Place `top-hub.json` in `public/data/` (ensure valid JSON)
- [ ] Add `TopHubSignalCard.tsx` and import into dashboard
- [ ] Run `npm run build` (or equivalent) — verify no hydration/runtime errors
- [ ] Open `/quality` (or dashboard route) — confirm card renders with data
- [ ] Disable any network writes (no POST/PUT from this component) — honors “Sense + Signal”
- [ ] Confirm no auth headers or tokens used in fetch

---

### 7) Notes & invariants

- **Zero writes**: component is strictly read-only; data updates happen via external pipeline (knowledge-rag).
- **No API rate limits**: uses static CDN path (`/data/top-hub.json`) — bypasses HF API/auth entirely.
- **Fast iteration**: update `top-hub.json` to refresh card without redeploying frontend.
- **Accessibility**: semantic `dl/dt/dd`, color + text status indicators, focus-safe structure.
