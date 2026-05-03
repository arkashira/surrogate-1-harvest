# Costinel / frontend

## Final synthesized implementation

**Goal (deliverable in <2h):** Add a “Top-Hub Signal” card to the Costinel dashboard that surfaces the most-connected hub (MOC) and 3 actionable, related docs from knowledge-rag. Pure frontend; no backend changes. Works when the endpoint exists and when it doesn’t.

---

## 1. API contract and types

Use a single, minimal, correct contract. Prefer `snippet` (not `summary`) and include optional `score` for future ranking.

**File:** `src/types/sense.ts`
```ts
export interface RelatedDoc {
  title: string;
  url: string;
  snippet?: string;
  score?: number;
}

export interface TopHubSignal {
  hub: {
    id: string;
    label: string;
    description?: string;
    connections?: number;
  };
  relatedDocs: RelatedDoc[];
  generatedAt?: string;
}
```

---

## 2. Data hook (robust, non-noisy)

- Use SWR for caching and revalidation.
- 60s refresh (fast enough for signals; avoids polling noise).
- On 404, stop retrying and keep fallback.
- On other errors, retry with backoff but don’t spam.
- Always return safe fallback data so UI renders useful content immediately.

**File:** `src/hooks/useTopHubSignal.ts`
```ts
import useSWR from 'swr';
import { TopHubSignal } from '@/types/sense';

const ENDPOINT = '/api/v1/sense/top-hub-signal';

const fallbackData: TopHubSignal = {
  hub: {
    id: 'MOC',
    label: 'MOC',
    description: 'Most-connected operational hub',
    connections: 42,
  },
  relatedDocs: [
    {
      title: 'Cost governance playbook — MOC',
      url: 'https://docs.axentx/cost-governance-moc',
      snippet: 'Actions to control spend across MOC-linked accounts',
    },
    {
      "title": 'Anomaly review: Q2 spikes',
      url: 'https://docs.axentx/anomalies/q2-spikes',
      snippet: 'Top drivers and recommended signals',
    },
    {
      title: 'RI coverage quick-win',
      url: 'https://docs.axentx/recommendations/ri-coverage',
      snippet: 'Increase coverage with 3 targeted reservations',
    },
  ],
  generatedAt: new Date().toISOString(),
};

const fetcher = (url: string) => fetch(url).then((r) => {
  if (!r.ok) {
    const err: any = new Error(r.statusText);
    err.status = r.status;
    throw err;
  }
  return r.json();
});

export function useTopHubSignal() {
  const { data, error, isLoading } = useSWR<TopHubSignal>(ENDPOINT, fetcher, {
    fallbackData,
    refreshInterval: 60_000,
    revalidateOnFocus: false,
    onErrorRetry: (err, _key, _config, revalidate, { retryCount }) => {
      // Stop retrying on 404 (endpoint absent)
      if (err?.status === 404) return;
      // Exponential backoff, capped
      if (retryCount >= 3) return;
      setTimeout(() => revalidate({ retryCount }), Math.min(3000, 1000 * 2 ** retryCount));
    },
  });

  return {
    data: data || fallbackData,
    error,
    isLoading,
    isError: !!error,
  };
}
```

---

## 3. Component (accessible, responsive, non-noisy)

- Use existing design tokens (Tailwind + Costinel tokens).
- Show inline loading skeleton, error state, and empty docs state.
- Links open in new tab with security attributes.
- Subtle attention cue on the hub badge; avoid distracting animations.
- Keep related docs list to max 3 items.

**File:** `src/components/dashboard/TopHubSignalCard.tsx`
```tsx
import { ExternalLink } from 'lucide-react';
import { TopHubSignal } from '@/types/sense';

interface TopHubSignalCardProps {
  data: TopHubSignal;
  loading?: boolean;
  error?: Error | null;
}

export function TopHubSignalCard({ data, loading, error }: TopHubSignalCardProps) {
  if (error) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
        Unable to load top-hub signal.
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
      {/* Header */}
      <div className="mb-4">
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center rounded-md bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-700 ring-1 ring-inset ring-indigo-600/10">
            Top hub
          </span>
          {loading && <span className="ml-2 h-2 w-2 animate-pulse rounded-full bg-gray-300" />}
        </div>

        <h3 className="mt-2 text-xl font-semibold text-gray-900">{data.hub.label}</h3>
        {data.hub.description && (
          <p className="mt-1 text-sm text-gray-600">{data.hub.description}</p>
        )}
        {typeof data.hub.connections === 'number' && (
          <p className="mt-1 text-xs text-gray-500">{data.hub.connections} connections</p>
        )}
      </div>

      {/* Related docs */}
      <div className="space-y-2">
        <h4 className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Related signals
        </h4>

        {loading ? (
          <ul className="space-y-2" aria-busy="true">
            {[1, 2, 3].map((i) => (
              <li key={i} className="flex items-start gap-2 rounded-md p-2">
                <div className="h-4 w-32 flex-none rounded bg-gray-100" />
              </li>
            ))}
          </ul>
        ) : (
          <ul className="space-y-2" aria-label="Related documents">
            {data.relatedDocs.slice(0, 3).map((doc, i) => (
              <li key={i}>
                <a
                  href={doc.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group flex items-start gap-2 rounded-md p-2 text-sm hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
                >
                  <span className="flex-1 truncate text-gray-700 group-hover:text-indigo-700">
                    {doc.title}
                  </span>
                  <ExternalLink className="mt-0.5 h-4 w-4 flex-none text-gray-400 group-hover:text-indigo-500" />
                </a>
                {doc.snippet && (
                  <p className="ml-2 text-xs text-gray-500">{doc.snippet}</p>
                )}
              </li>
            ))}

            {data.relatedDocs.length === 0 && (
              <li className="text-sm text-gray-500">No related signals available.</li>
            )}
          </ul>
        )}
      </div>

      {data.generatedAt && (
        <p className="mt-4 text-xs text-gray-400">
          Updated {new Date(data.generatedAt).toLocaleString()}
        </p>
      )}
    </div>
  );
}
```

---

## 4. Mount on dashboard

Place the card in the main grid. Adjust spans to match your existing layout; the example below uses a 3-column grid and places the card in the right column on large screens.

