# Costinel / discovery

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Highest-value improvement**: Add a resilient “Top Hub” panel to Costinel’s dashboard that surfaces the most-connected hub (e.g., “MOC”) and related docs using **CDN-only fetches**, zero model compute on client, and graceful fallbacks.

### Why this ships fast
- Pure frontend addition (React + Tailwind) + one small Node helper.
- Uses public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) — no auth, no rate-limit risk during runtime.
- Reuses existing `knowledge-rag` file list pattern: pre-list once, embed JSON, fetch via CDN at runtime.
- No backend changes, no training, no GPU, no cron — safe and non-breaking.

---

### Steps (timeboxed)

1. **Add precomputed top-hub index** (5 min)  
   Create `public/data/top-hub-index.json` (committed) containing:
   - `hubId`, `hubName`, `hubSlug`
   - `relatedDocs[]` with `{ title, slug, cdnUrl, summary }`
   - `lastUpdatedISO`

   This file is the single source of truth; updated by ops/scripts later (not in this ticket).

2. **Create CDN fetch utility** (10 min)  
   Add `src/lib/cdn.ts`:
   - `fetchTopHubIndex()` — fetches `public/data/top-hub-index.json` (CDN path if deployed under CDN).
   - `fetchRelatedDocContent(slug)` — optional, uses `cdnUrl` for raw content if needed.
   - Exponential backoff + timeout + graceful `null` returns.

3. **Add TopHubPanel component** (25 min)  
   Create `src/components/TopHubPanel.tsx`:
   - SSR-safe: client-side fetch in `useEffect`.
   - Skeleton loader while fetching.
   - Error boundary: show cached fallback or friendly message.
   - Display:
     - Hub name + badge (e.g., “MOC”).
     - Short insight (from index).
     - List of related docs as clickable cards (title + summary).
   - Tags: `#knowledge-rag #graph #hub`.

4. **Wire into dashboard** (10 min)  
   Import `TopHubPanel` into main dashboard layout (likely `src/pages/Dashboard.tsx` or equivalent).
   - Place in high-visibility zone (top-right or sidebar).
   - Respect existing grid system.

5. **Add tests & lint** (10 min)  
   - Basic unit test for CDN utility (mock fetch).
   - Component smoke test.
   - Ensure no console errors.

6. **Verify CDN path & deploy** (10 min)  
   - Confirm `public/data/top-hub-index.json` is served at `/data/top-hub-index.json`.
   - Local dev + staging smoke test.
   - Commit and ship.

---

### Code snippets

#### public/data/top-hub-index.json
```json
{
  "hubId": "moc",
  "hubName": "MOC",
  "hubSlug": "moc",
  "summary": "Most-connected hub for cost governance signals and cross-cloud recommendations.",
  "relatedDocs": [
    {
      "title": "Cost Anomaly Playbook",
      "slug": "cost-anomaly-playbook",
      "cdnUrl": "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/docs/cost-anomaly-playbook.md",
      "summary": "Detect, triage, and signal cost anomalies across multi-cloud environments."
    },
    {
      "title": "RI Coverage Analysis Guide",
      "slug": "ri-coverage-guide",
      "cdnUrl": "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/docs/ri-coverage-guide.md",
      "summary": "Actionable guidance for reserved instance planning and coverage gaps."
    }
  ],
  "lastUpdatedISO": "2026-05-03T08:00:00.000Z"
}
```

#### src/lib/cdn.ts
```ts
const INDEX_PATH = '/data/top-hub-index.json';
const TIMEOUT_MS = 5000;

async function fetchWithTimeout(url: string): Promise<Response> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    return await fetch(url, { signal: controller.signal });
  } finally {
    clearTimeout(id);
  }
}

export async function fetchTopHubIndex(): Promise<TopHubIndex | null> {
  try {
    const res = await fetchWithTimeout(INDEX_PATH);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as TopHubIndex;
  } catch (err) {
    console.warn('[TopHub] CDN fetch failed, using fallback', err);
    return null;
  }
}

export interface TopHubIndex {
  hubId: string;
  hubName: string;
  hubSlug: string;
  summary: string;
  relatedDocs: Array<{
    title: string;
    slug: string;
    cdnUrl: string;
    summary: string;
  }>;
  lastUpdatedISO: string;
}
```

#### src/components/TopHubPanel.tsx
```tsx
import { useEffect, useState } from 'react';
import { fetchTopHubIndex, type TopHubIndex } from '../lib/cdn';

export function TopHubPanel() {
  const [data, setData] = useState<TopHubIndex | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubIndex().then((res) => {
      if (mounted) {
        setData(res);
        setLoading(false);
      }
    });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-white p-4 shadow-sm">
        <div className="h-6 w-32 bg-gray-200 rounded animate-pulse mb-2" />
        <div className="h-4 w-48 bg-gray-100 rounded animate-pulse mb-4" />
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="h-10 bg-gray-50 rounded animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border bg-white p-4 shadow-sm text-sm text-gray-500">
        Top hub insights unavailable.
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-semibold text-gray-900">Top Hub</h3>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700">
          {data.hubName}
        </span>
      </div>
      <p className="text-sm text-gray-600 mb-3">{data.summary}</p>
      <ul className="space-y-2">
        {data.relatedDocs.map((doc) => (
          <li key={doc.slug}>
            <a
              href={doc.cdnUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="block rounded-md border p-2 hover:border-blue-200 hover:bg-blue-50 transition-colors"
            >
              <div className="font-medium text-sm text-gray-900">{doc.title}</div>
              <div className="text-xs text-gray-500 mt-0.5">{doc.summary}</div>
            </a>
          </li>
        ))}
      </ul>
      <div className="mt-2 text-xs text-gray-400">
        Updated {new Date(data.lastUpdatedISO).toLocaleDateString()}
      </div>
    </div>

