# Costinel / frontend

## Implementation Plan — Top Hub Signal Panel (CDN-first)

**Scope**: Frontend-only addition to Costinel dashboard.  
**Effort**: ~60–90 minutes.  
**Mechanism**: CDN JSON fetch (no auth, no backend) with local fallback and client cache.  
**Goal**: Show the most-connected hub (e.g., "MOC") and top 3 related signals from the knowledge graph to surface contextual insights before planning tasks.

### 1) File changes
- `src/components/TopHubSignalPanel.tsx` — new component (CDN fetch + render).
- `src/pages/Dashboard.tsx` — import and mount panel near top of dashboard.
- `public/data/top-hub-fallback.json` — local fallback payload (committed).
- `src/lib/cdn.ts` — tiny CDN fetcher with cache + timeout.

### 2) CDN payload contract (public JSON)
URL (example):  
`https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/latest.json`

Shape:
```json
{
  "hub": "MOC",
  "score": 0.94,
  "updatedAt": "2026-05-03T04:45:00Z",
  "signals": [
    { "id": "s1", "title": "RI coverage gap", "impact": "high", "context": "us-east-1 m5.xlarge 32% under-covered" },
    { "id": "s2", "title": "Orphaned EBS trend", "impact": "medium", "context": "120GB across 3 accounts" },
    { "id": "s3", "title": "Nightly idle CICD runners", "impact": "low", "context": "est. $180/mo savings" }
  ]
}
```

### 3) Fetch strategy (CDN-first, zero-auth)
- Use `fetch` without Authorization header to bypass HF API rate limits.
- Single CDN GET per page load; cache in `sessionStorage` for 10 minutes to avoid bursts.
- Fast fail to bundled fallback if CDN unavailable or malformed.

### 4) UI/UX
- Small elevated card at top of dashboard under main KPI row.
- Hub name + score (pill), updated timestamp, 3 signals as compact list with impact badges.
- Skeleton while loading; subtle retry on soft failure (no noisy modals).

---

## Code snippets

### `src/lib/cdn.ts`
```ts
const CDN_URL = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/latest.json';
const CACHE_KEY = 'costinel:top-hub:payload';
const CACHE_TTL_MS = 10 * 60 * 1000; // 10m

export interface TopHubSignal {
  id: string;
  title: string;
  impact: 'low' | 'medium' | 'high';
  context: string;
}

export interface TopHubPayload {
  hub: string;
  score: number;
  updatedAt: string;
  signals: TopHubSignal[];
}

async function fetchFromCDN(timeoutMs = 4000): Promise<TopHubPayload | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(CDN_URL, {
      method: 'GET',
      signal: controller.signal,
      // intentionally no Authorization header to bypass HF API auth/rate-limit
      cache: 'no-store',
    });
    clearTimeout(timer);

    if (!res.ok) throw new Error(`CDN ${res.status}`);
    const json = (await res.json()) as TopHubPayload;
    if (!json?.hub || !Array.isArray(json.signals)) throw new Error('Invalid payload shape');
    return json;
  } catch {
    return null;
  }
}

function getCached(): TopHubPayload | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { ts, payload } = JSON.parse(raw);
    if (Date.now() - ts > CACHE_TTL_MS) return null;
    return payload as TopHubPayload;
  } catch {
    return null;
  }
}

function setCached(payload: TopHubPayload) {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), payload }));
  } catch {
    // ignore storage errors
  }
}

export async function fetchTopHubPayload(localFallback: TopHubPayload): Promise<TopHubPayload> {
  const cached = getCached();
  if (cached) return cached;

  const remote = await fetchFromCDN();
  if (remote) {
    setCached(remote);
    return remote;
  }

  // soft fallback (no cache) so UI still shows something useful
  return localFallback;
}
```

### `public/data/top-hub-fallback.json`
```json
{
  "hub": "MOC",
  "score": 0.92,
  "updatedAt": "2026-05-03T04:00:00Z",
  "signals": [
    { "id": "s1", "title": "RI coverage gap", "impact": "high", "context": "us-east-1 m5.xlarge 32% under-covered" },
    { "id": "s2", "title": "Orphaned EBS trend", "impact": "medium", "context": "120GB across 3 accounts" },
    { "id": "s3", "title": "Nightly idle CICD runners", "impact": "low", "context": "est. $180/mo savings" }
  ]
}
```

### `src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import { fetchTopHubPayload, TopHubPayload } from '../lib/cdn';
import fallback from '../data/top-hub-fallback.json';

const impactColors = {
  low: 'bg-gray-100 text-gray-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-red-100 text-red-800',
} as const;

export default function TopHubSignalPanel() {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetchTopHubPayload(fallback as TopHubPayload).then((p) => {
      if (mounted) {
        setPayload(p);
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
        <div className="flex items-center gap-3">
          <div className="h-8 w-24 animate-pulse rounded bg-gray-200" />
          <div className="h-6 w-16 animate-pulse rounded bg-gray-200" />
        </div>
        <div className="mt-3 space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-10 animate-pulse rounded bg-gray-50" />
          ))}
        </div>
      </div>
    );
  }

  if (!payload) return null;

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-gray-900">{payload.hub}</span>
          <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-800">
            {Math.round(payload.score * 100)}%
          </span>
        </div>
        <time className="text-xs text-gray-500" dateTime={payload.updatedAt}>
          Updated {new Date(payload.updatedAt).to
