# Costinel / backend

## Final Synthesis: CDN-First Top-Hub Signal Panel (Production-Ready)

**Core improvement**: Embed a resilient “Top Hub” panel into Costinel’s dashboard that surfaces the most-connected hub (e.g., “MOC”) and related docs using **CDN-only fetches**, zero model compute, and graceful fallback when CDN/files unavailable.

---

### Why this now (merged rationale)
- Uses existing knowledge-rag graph outputs (`granite-business-research.sh` + `knowledge-rag`).
- Avoids API/auth/rate-limit by fetching static JSON via CDN (`/resolve/main/...`).
- Pure frontend addition (no backend changes) → <2h.
- Aligns with pattern: review top-hub before planning tasks (#knowledge-rag #graph #hub).
- Follows Costinel’s “Sense + Signal — Execute” philosophy: surface insight, don’t mutate state.

---

### Concrete Implementation Plan (1h 45m)

**1. Define CDN file contract & paths (5m)**
- Primary dataset repo (recommended):  
  `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/knowledge-rag/top-hub.json`  
  `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/knowledge-rag/related-docs.json`
- Fallback/alternate: repo-relative `public/data/top-hub.json` served via CDN.

**2. Create JSON payloads (10m)**
- `top-hub.json`:
  ```json
  {
    "hub_id": "MOC",
    "title": "MOC",
    "score": 0.94,
    "summary": "Most-connected operational hub. Review before planning tasks to align with knowledge-rag signals.",
    "tags": ["knowledge-rag", "graph", "hub"],
    "updated_at": "2026-05-03T04:45:00Z"
  }
  ```
- `related-docs.json`:
  ```json
  [
    {
      "id": "2026-04-27_top_hub",
      "title": "Top-hub doc insight (2026-04-27)",
      "url": "https://github.com/AXENTX/Costinel/discussions/123",
      "snippet": "Review the most-connected hub (e.g., MOC) before planning tasks",
      "tags": ["knowledge-rag", "graph", "hub"]
    },
    {
      "id": "20260503-044500_Costinel_discovery",
      "title": "Costinel / discovery",
      "url": "https://github.com/AXENTX/Costinel/discussions/124",
      "snippet": "Implementation plan for CDN-first top-hub signal panel",
      "tags": ["knowledge-rag", "graph"]
    }
  ]
  ```

**3. Add resilient fetch utility (15m)**
- `src/lib/fetch-cdn-json.js`
  ```js
  const CDN_BASE = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main';

  async function fetchCdnJson(path, { timeout = 4000, retries = 1 } = {}) {
    const url = `${CDN_BASE}/${path.replace(/^\/+/, '')}`;
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);

    try {
      const res = await fetch(url, { signal: controller.signal, cache: 'no-store' });
      clearTimeout(id);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      clearTimeout(id);
      if (retries > 0) return fetchCdnJson(path, { timeout, retries: retries - 1 });
      console.warn('[CDN fetch failed]', path, err.message);
      return null;
    }
  }

  export async function fetchTopHub() {
    return fetchCdnJson('knowledge-rag/top-hub.json');
  }

  export async function fetchRelatedDocs() {
    return fetchCdnJson('knowledge-rag/related-docs.json');
  }
  ```

**4. Add panel UI component (40m)**
- Location: `/src/components/TopHubPanel.tsx` (or `.vue` / `.jsx`).
- Behavior:
  - Fetch both JSON files via CDN URLs.
  - Cache in `localStorage` with 10-minute TTL to avoid repeated fetches.
  - If CDN fails, render last cached data; if none, render empty state with subtle message.
- Visual: card with hub title, score meter, summary, and list of related docs with links.

Example (React):
```tsx
import { useEffect, useState } from 'react';
import { fetchTopHub, fetchRelatedDocs } from '../lib/fetch-cdn-json';

const CACHE_TTL = 10 * 60 * 1000; // 10m

function useCdnTopHub() {
  const [data, setData] = useState<{ hub?: any; docs?: any[] }>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        const cached = localStorage.getItem('costinel:top-hub');
        const parsed = cached ? JSON.parse(cached) : null;
        const expired = parsed && Date.now() - parsed.ts > CACHE_TTL;

        if (parsed && !expired) {
          setData(parsed.payload);
          setLoading(false);
        }

        const [hub, docs] = await Promise.all([fetchTopHub(), fetchRelatedDocs()]);
        const payload = { hub: hub || parsed?.payload?.hub, docs: docs || parsed?.payload?.docs };
        localStorage.setItem('costinel:top-hub', JSON.stringify({ ts: Date.now(), payload }));
        setData(payload);
      } catch (e) {
        // keep cached if available
      } finally {
        setLoading(false);
      }
    };

    load();
  }, []);

  return { ...data, loading };
}

export default function TopHubPanel() {
  const { hub, docs, loading } = useCdnTopHub();

  if (loading && !hub) return <div className="p-4 text-sm text-gray-400">Loading insights…</div>;
  if (!hub) return null;

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-semibold">Top Hub</h3>
        <span className="rounded bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-800">
          {hub.score}
        </span>
      </div>
      <h4 className="mb-2 text-lg font-medium">{hub.title}</h4>
      <p className="mb-3 text-sm text-gray-600">{hub.summary}</p>
      {docs && docs.length > 0 && (
        <ul className="space-y-1">
          {docs.map((doc) => (
            <li key={doc.id}>
              <a
                href={doc.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-600 hover:underline"
              >
                {doc.title}
              </a>
              <p className="text-xs text-gray-500">{doc.snippet}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

**5. Integrate into dashboard layout (15m)**
- Insert `<TopHubPanel />` in the main dashboard grid (e.g., right sidebar or top row).
- Ensure responsive behavior (collapses to icon-only on mobile).

**6. Testing & verification (15m)**
- Verify panel renders with mock CDN URLs.
- Simulate CDN 404 and ensure graceful fallback.
- Confirm no console errors and Lighthouse accessibility checks pass.

**7. Deployment checklist (5m)**
- Ensure JSON files are committed to dataset repo and published to CDN.
- Confirm public read access on `resolve/main/`
