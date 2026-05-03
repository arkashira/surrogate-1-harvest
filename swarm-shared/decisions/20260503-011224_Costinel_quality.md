# Costinel / quality

**Final synthesized implementation (correct + actionable)**

**Goal (deliverable in <2h)**  
Add a “Top-Hub Signal” card to the Costinel dashboard that surfaces the most-connected hub (MOC) and 3 related actionable documents from knowledge-RAG.

**Key synthesis decisions**  
- **Frontend-only implementation** (Candidate 3) is the highest-value, lowest-risk path under 2 hours.  
- **Assume `/api/v1/sense/top-hub-signal` already exists** (Candidates 1/2) or falls back to a local mock so the card works immediately.  
- **No backend changes required now**; if the endpoint is missing, add a minimal mock route or JSON file rather than building full orchestration.  
- **Design and motion** must match the “Sense + Signal — ไม่ Execute” tone and existing design tokens.

---

### 1) Create the component (60–75 min)

`src/components/dashboard/TopHubSignalCard.tsx`

```tsx
import React from "react";
import useSWR from "swr";

interface RelatedDoc {
  title: string;
  url?: string;
  content?: string;
  source?: string;
}

interface TopHubSignal {
  hub: string;
  insight?: string;
  relatedDocs: RelatedDoc[];
  generatedAt?: string;
}

const API_PATH = "/api/v1/sense/top-hub-signal";

const fetcher = (url: string) => fetch(url).then((r) => r.json());

const mockResponse: TopHubSignal = {
  hub: "MOC",
  insight: "Highest-cost connector; prioritize policy and vendor review this quarter.",
  relatedDocs: [
    { title: "MOC Cost Drivers 2024", url: "#", source: "Finance" },
    { title: "Vendor Contract Review Checklist", url: "#", source: "Procurement" },
    { title: "Optimization Playbook: Connectivity", url: "#", source: "Engineering" },
  ],
};

const TopHubSignalCard: React.FC = () => {
  const { data, error, isLoading } = useSWR<TopHubSignal>(API_PATH, fetcher, {
    fallbackData: mockResponse,
    revalidateOnMount: true,
    revalidateIfStale: false,
  });

  const hub = data?.hub ?? "—";
  const insight = data?.insight ?? "";
  const docs = data?.relatedDocs ?? [];

  if (error && !isLoading) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        Unable to load Top-Hub Signal.
      </div>
    );
  }

  return (
    <article className="rounded-lg border bg-white p-5 shadow-sm">
      <div className="mb-3 flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-amber-100 text-amber-700">
            <svg
              className="h-4 w-4"
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
          </span>
          <div>
            <h3 className="text-sm font-semibold text-slate-800">Top-Hub Signal</h3>
            <p className="text-xs text-slate-500">Sense + Signal — ไม่ Execute</p>
          </div>
        </div>
        {data?.generatedAt && (
          <time className="text-xs text-slate-400" dateTime={data.generatedAt}>
            {new Date(data.generatedAt).toLocaleDateString()}
          </time>
        )}
      </div>

      {isLoading && !data ? (
        <div className="space-y-3">
          <div className="h-5 w-32 animate-pulse rounded bg-slate-200" />
          <div className="h-4 w-full animate-pulse rounded bg-slate-100" />
          <div className="flex gap-2">
            <div className="h-7 w-20 animate-pulse rounded-full bg-slate-100" />
            <div className="h-7 w-20 animate-pulse rounded-full bg-slate-100" />
            <div className="h-7 w-20 animate-pulse rounded-full bg-slate-100" />
          </div>
        </div>
      ) : (
        <>
          <div className="mb-3">
            <p className="text-xs uppercase tracking-wide text-slate-500">Most-connected hub</p>
            <p className="text-xl font-semibold text-slate-900">{hub}</p>
          </div>

          {insight && (
            <p className="mb-3 text-sm text-slate-600">{insight}</p>
          )}

          <div className="flex flex-wrap gap-2" role="list" aria-label="Related documents">
            {docs.slice(0, 3).map((doc, i) => (
              <a
                key={i}
                href={doc.url || "#"}
                onClick={(e) => !doc.url && e.preventDefault()}
                className={`
                  inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium transition
                  ${doc.url
                    ? "border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100"
                    : "border-slate-200 bg-slate-50 text-slate-600 cursor-default"
                  }
                `}
                aria-label={`Document: ${doc.title}`}
              >
                <span className="truncate">{doc.title}</span>
                {doc.source && (
                  <span className="rounded bg-white/60 px-1 text-[10px] font-semibold text-slate-400">
                    {doc.source}
                  </span>
                )}
              </a>
            ))}
          </div>
        </>
      )}
    </article>
  );
};

export default TopHubSignalCard;
```

---

### 2) Add to the dashboard layout (10–15 min)

Insert into the main grid (example using Tailwind grid):

```tsx
// Inside your dashboard page/component
<TopHubSignalCard />
```

Tailwind grid example:

```tsx
<div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
  <div className="xl:col-span-2">{/* primary panels */}</div>
  <div className="xl:col-span-2">
    <TopHubSignalCard />
  </div>
</div>
```

Responsive behavior: full width on mobile, spans appropriate columns on desktop.

---

### 3) Fallback/mock strategy (immediate)

- The component uses `fallbackData` (mock) so it renders instantly if the endpoint is missing.  
- If you want a lightweight backend mock for development, add a static JSON route or a minimal Next.js API route:

`pages/api/v1/sense/top-hub-signal.ts` (Next.js example)

```ts
import type { NextApiRequest, NextApiResponse } from "next";

export default function handler(_: NextApiRequest, res: NextApiResponse) {
  res.status(200).json({
    hub: "MOC",
    insight: "Highest-cost connector; prioritize policy and vendor review this quarter.",
    relatedDocs: [
      { title: "MOC Cost Drivers 2024", url: "#", source: "Finance" },
      { title: "Vendor Contract Review Checklist", url: "#", source: "Procurement" },
      { title: "Optimization Playbook: Connectivity", url: "#", source: "Engineering" },
   
