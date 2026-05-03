# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Single highest-value deliverable
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects the most-connected hub (default: `MOC`) from knowledge-rag graph metadata
- Shows 3 contextual insights (title + short summary)
- Uses CDN-first data fetching (bypasses HF API rate limits)
- Renders in <100ms, zero impact on dashboard interactivity
- Fails gracefully (renders nothing; never blocks hydration or interactivity)

### Architecture (CDN-first)
```
Costinel Dashboard (Next.js/React)
  └─ useSWR (client) → CDN JSON
        https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/knowledge-rag/top-hub.json
        ├─ hub: "MOC"
        ├─ degree: 142
        └─ insights: [{title, summary, relevance}]
```

### Concrete file changes (minimal surface)
1. `src/components/TopHubSignalPanel.tsx` — new component (client-only, non-blocking)
2. `src/lib/cdn.ts` — CDN fetch utilities with localStorage cache + graceful fallbacks
3. Dashboard layout: insert `<TopHubSignalPanel />` in sidebar/top bar (non-blocking grid cell)

### Implementation Steps (ordered for <2h delivery)

1. **Create the CDN JSON once** (Mac, after rate-limit window)  
   - Run: `list_repo_tree(path='knowledge-rag/', recursive=False)`  
   - Produce `knowledge-rag/top-hub.json` with shape:
     ```json
     {
       "hub": "MOC",
       "degree": 142,
       "insights": [
         { "title": "MOC: Central router", "summary": "Handles 42% of cross-graph traffic...", "relevance": 0.92 },
         { "title": "Latency profile", "summary": "Median 38ms p95 110ms...", "relevance": 0.87 },
         { "title": "Risk signals", "summary": "Two high-degree dependents show churn...", "relevance": 0.81 }
       ],
       "generatedAt": "2026-05-03T03:00:00Z"
     }
     ```
   - Commit or CI-generate this file nightly.

2. **Add CDN fetch util** (`src/lib/cdn.ts`)  
   - Use `useSWR`-friendly fetcher with:
     - `localStorage` cache (10m TTL)
     - `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/knowledge-rag/top-hub.json`
     - No Authorization header
     - Graceful `null` on 429/404/network errors
   - Expose:
     - `fetchTopHubManifest(): Promise<TopHubManifest | null>`
     - `useTopHubManifest(): SWRResponse<TopHubManifest | null>`

3. **Create TopHubSignalPanel** (`src/components/TopHubSignalPanel.tsx`)  
   - Client-only (`'use client'`)
   - Uses `useTopHubManifest()` (SWR)
   - Skeleton loader while fetching
   - If data missing or error: **return null** (non-blocking)
   - Render:
     - Hub badge + degree
     - 3 insights (title + summary, line-clamp)
     - Updated timestamp
   - Minimal CSS; no layout shift.

4. **Wire into dashboard**  
   - Insert `<TopHubSignalPanel />` in sidebar or top bar grid cell.
   - Ensure it’s outside SSR-critical path (no impact on hydration).
   - Confirm Lighthouse shows no blocking requests.

5. **CI/CD (optional but recommended)**  
   - Nightly GitHub Action or Mac cron:
     - Regenerate `knowledge-rag/top-hub.json` using HF API (once, after rate-limit window)
     - Commit and push
   - Keep action simple: one HF API call + commit.

6. **Testing & rollout checklist**
   - Verify CDN URL resolves without auth.
   - Simulate 429/404/offline: panel must return null (no errors in UI).
   - Measure TTI: panel fetch must not delay dashboard interactivity.
   - Lighthouse: no new blocking resources.

---

### Resolved contradictions (correctness + actionability)
- **API vs CDN**: Use CDN directly in component (no extra API route). Avoids extra infra, latency, and auth surface. If server-side composition is needed later, add lightweight route then; start simple.
- **File location**: Use `knowledge-rag/top-hub.json` (not root) to match repo structure and avoid collisions.
- **Fetch strategy**: Prefer `useSWR` + localStorage over one-time `useEffect` for better caching, revalidation, and error handling.
- **Graceful failure**: Return `null` (not empty panel or console-only warnings) to ensure zero UI impact.
- **CI scope**: Keep CI to regenerating the manifest only; do not bake complex build steps.

---

### Code snippets (final)

#### `src/lib/cdn.ts`
```ts
const REPO = 'axentx/knowledge-rag';
const CACHE_TTL = 10 * 60 * 1000; // 10m

export interface Insight {
  title: string;
  summary: string;
  relevance?: number;
}

export interface TopHubManifest {
  hub: string;
  degree?: number;
  insights: Insight[];
  generatedAt: string;
}

async function fetchCDN<T = unknown>(path: string): Promise<T | null> {
  const url = `https://huggingface.co/datasets/${REPO}/resolve/main/${path}`;
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

function getCached<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const { data, ts } = JSON.parse(raw);
    if (Date.now() - ts > CACHE_TTL) return null;
    return data as T;
  } catch {
    return null;
  }
}

function setCached<T>(key: string, data: T) {
  try {
    localStorage.setItem(key, JSON.stringify({ data, ts: Date.now() }));
  } catch {
    // ignore storage errors
  }
}

const MANIFEST_KEY = 'top-hub-manifest';
const MANIFEST_PATH = 'knowledge-rag/top-hub.json';

export async function fetchTopHubManifest(): Promise<TopHubManifest | null> {
  const cached = getCached<TopHubManifest>(MANIFEST_KEY);
  if (cached) return cached;

  const data = await fetchCDN<TopHubManifest>(MANIFEST_PATH);
  if (data) setCached(MANIFEST_KEY, data);
  return data;
}
```

#### `src/components/TopHubSignalPanel.tsx`
```tsx
'use client';

import useSWR from 'swr';
import { fetchTopHubManifest, type TopHubManifest } from '@/lib/cdn';

const fetcher = () => fetchTopHubManifest();

export function TopHubSignalPanel() {
  const { data, error, isLoading } = useSWR('top-hub-manifest', fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    dedupingInterval: 60000,
  });

  const manifest: TopHubManifest | null = error ? null : data || null;

  if (isLoading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <div className="h-5 w-32 animate-pulse rounded bg-muted mb-3" />
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-12 animate-pulse rounded bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  if (!manifest?.hub || !
