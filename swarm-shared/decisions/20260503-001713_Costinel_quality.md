# Costinel / quality

### Final Implementation Plan — Costinel “Top-Hub Signal” Card (Read-Only)

**Scope**: ≤2h, strictly read-only frontend card  
**Principle**: “Sense + Signal — ไม่ Execute” (no writes, no runtime mutations, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with contextual, actionable insights from knowledge-rag.

---

### 1) Chosen approach (merged strengths)
- Use **static JSON produced offline by knowledge-rag** (Candidate 2) for reliability, speed, and zero runtime API/mutations.
- Keep the **component interface simple and presentational** (Candidate 1 + Candidate 2) and reuse existing design tokens/primitives.
- Render: hub name, degree, description, top 5 related docs (title + snippet + link), and last-updated timestamp.
- No client-side state changes; links use `target="_self"` (in-app navigation) to avoid new tabs unless your app convention requires otherwise.

---

### 2) Concrete file changes

#### A) Static data (knowledge-rag output)
Path: `public/data/top-hub.json`

```json
{
  "hub": "MOC",
  "degree": 42,
  "description": "Multi-cloud observability & cost governance nexus",
  "relatedDocs": [
    {
      "title": "Cost Anomaly Detection Playbook",
      "snippet": "Detect spend spikes across AWS/GCP/Azure using streaming baselines...",
      "url": "/docs/playbooks/cost-anomaly-detection"
    },
    {
      "title": "RI Coverage Analysis Guide",
      "snippet": "How to size reservations and measure coverage gaps by account and service...",
      "url": "/docs/guides/ri-coverage"
    },
    {
      "title": "Tag Governance Policy",
      "snippet": "Required tags, enforcement rules, and exception workflows for cost allocation...",
      "url": "/docs/policies/tag-governance"
    },
    {
      "title": "Forecasting Model Notes",
      "snippet": "Time-series approach and feature set used for 30-day spend forecasts...",
      "url": "/docs/models/forecasting"
    },
    {
      "title": "Audit Trail Specification",
      "snippet": "Immutable log schema for proposals, signals, and human decisions...",
      "url": "/docs/specs/audit-trail"
    }
  ],
  "updatedAt": "2026-05-03T08:00:00Z"
}
```

---

#### B) Presentational card component
Path: `src/components/cards/TopHubSignalCard.tsx`

```tsx
import React from 'react';
import { CalendarIcon, LinkIcon } from '@heroicons/react/20/solid';
import hubData from '../../data/top-hub.json';

type RelatedDoc = {
  title: string;
  snippet: string;
  url: string;
};

type TopHubData = {
  hub: string;
  degree: number;
  description: string;
  relatedDocs: RelatedDoc[];
  updatedAt: string;
};

const TopHubSignalCard: React.FC = () => {
  const data = hubData as TopHubData;

  return (
    <section
      className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm"
      aria-label={`Top hub signal: ${data.hub}`}
    >
      {/* Header */}
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-900">Top-Hub Signal</h2>
          <p className="mt-1 text-sm text-gray-500">Most-connected hub by graph degree</p>
        </div>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700">
          {data.hub}
        </span>
      </div>

      {/* Hub summary */}
      <div className="mb-4 rounded-lg bg-gray-50 p-3">
        <p className="text-sm text-gray-600">{data.description}</p>
        <p className="mt-1 text-xs text-gray-500">
          Degree: <span className="font-mono text-gray-700">{data.degree}</span>
        </p>
      </div>

      {/* Related docs */}
      <div className="space-y-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-gray-500">Related docs</h3>
        <ul className="space-y-2" role="list">
          {data.relatedDocs.map((doc, idx) => (
            <li key={idx} className="group">
              <a
                href={doc.url}
                className="block rounded p-2 -m-2 text-sm transition-colors hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                target="_self"
                rel="noopener"
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="truncate font-medium text-gray-900 group-hover:text-blue-600">
                    {doc.title}
                  </span>
                  <LinkIcon className="mt-0.5 h-4 w-4 flex-none text-gray-400 group-hover:text-blue-500" />
                </div>
                <p className="mt-0.5 line-clamp-2 text-xs text-gray-600">{doc.snippet}</p>
              </a>
            </li>
          ))}
        </ul>
      </div>

      {/* Footer */}
      <div className="mt-4 flex items-center gap-1.5 text-xs text-gray-400">
        <CalendarIcon className="h-3.5 w-3.5" />
        <time dateTime={data.updatedAt}>
          Updated {new Date(data.updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
        </time>
      </div>
    </section>
  );
};

export default TopHubSignalCard;
```

---

#### C) Dashboard placement
Path: `src/pages/Dashboard.tsx` (or your main dashboard grid)

```tsx
import TopHubSignalCard from '../components/cards/TopHubSignalCard';

// Inside your dashboard grid:
<div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
  {/* Left column: Top-Hub Signal */}
  <div className="lg:col-span-1">
    <TopHubSignalCard />
  </div>

  {/* Right column: other widgets */}
  <div className="lg:col-span-2 space-y-6">
    {/* ...existing cards... */}
  </div>
</div>
```

---

### 3) Knowledge-rag integration note (read-only)
- The knowledge-rag pipeline should **produce** `public/data/top-hub.json` as a build/deploy step (or periodic offline job).  
- The frontend **only consumes** this static file — no runtime queries, no mutations, no writes.  
- If you prefer to fetch at runtime (still read-only), replace the local import with a `fetch('/data/top-hub.json')` in a `useEffect` and store in state; keep the same component API.

---

### 4) Acceptance criteria (fast validation)
- Card appears on dashboard showing hub name, degree, description, 5 docs, and updated timestamp.  
- No network requests from the client (if using static import) or only a single GET to the JSON file (if fetched).  
- No console errors; links navigate correctly within the app.  
- Styles consistent with existing UI tokens.  
- Total implementation time ≤2h.
