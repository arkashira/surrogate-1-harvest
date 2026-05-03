# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**.

### Scope (ship in <2h)
- Add a lightweight `TopHubPanel` component to the dashboard layout.
- Fetch baked hub data from public CDN (`/resolve/main/...`) — no auth, no API rate limits.
- Single pre-generated JSON file per date: `top-hub/{date}/hub.json` (produced by backend/knowledge-rag).
- Graceful fallback to local stub if CDN fails or 404.
- Polite refresh (once per mount) with 5-minute client cache (sessionStorage).
- No build changes; drop-in component.

### File changes
1. `src/components/TopHubPanel.tsx` — new component.
2. `src/pages/Dashboard.tsx` — mount panel in sidebar/header area.
3. `src/lib/cdn.ts` — tiny CDN fetcher with cache + timeout.
4. `src/stubs/top-hub.json` — local stub for fallback.

---

## Code Snippets

### 1) CDN fetcher (`src/lib/cdn.ts`)
```ts
// src/lib/cdn.ts
const CDN_ROOT = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main';
const CACHE_TTL = 5 * 60 * 1000; // 5m

export interface HubSignal {
  hub: string;
  label: string;
  score: number;
  updated_at: string; // ISO
  insights: string[];
  links?: Array<{ label: string; href: string }>;
}

async function getCached<T>(key: string): Promise<T | null> {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const { ts, value } = JSON.parse(raw);
    if (Date.now() - ts > CACHE_TTL) return null;
    return value as T;
  } catch {
    return null;
  }
}

async function setCached<T>(key: string, value: T) {
  try {
    sessionStorage.setItem(key, JSON.stringify({ ts: Date.now(), value }));
  } catch {
    // ignore storage limits
  }
}

export async function fetchTopHubSignal(dateFolder: string): Promise<HubSignal | null> {
  const cacheKey = `top-hub:${dateFolder}`;
  const cached = await getCached<HubSignal>(cacheKey);
  if (cached) return cached;

  const url = `${CDN_ROOT}/top-hub/${dateFolder}/hub.json`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);

  try {
    const res = await fetch(url, { signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok) throw new Error(`CDN ${res.status}`);
    const payload = await res.json();
    await setCached(cacheKey, payload);
    return payload;
  } catch {
    // fallback to local stub
    try {
      const mod = await import('../stubs/top-hub.json');
      await setCached(cacheKey, mod.default);
      return mod.default;
    } catch {
      return null;
    }
  }
}
```

### 2) Stub (`src/stubs/top-hub.json`)
```json
{
  "hub": "MOC",
  "label": "Most-Connected Hub",
  "score": 0.92,
  "updated_at": "2026-04-29T00:00:00Z",
  "insights": [
    "MOC shows strongest cross-cluster influence on cost governance signals.",
    "Recommended to prioritize policy reviews anchored to MOC lineage."
  ],
  "links": [
    { "label": "View lineage", "href": "/knowledge-rag/hubs/MOC" }
  ]
}
```

### 3) TopHubPanel component (`src/components/TopHubPanel.tsx`)
```tsx
// src/components/TopHubPanel.tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal, type HubSignal } from '../lib/cdn';
import { formatDistanceToNow } from 'date-fns';

function getTodayFolder(): string {
  const d = new Date();
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}

export default function TopHubPanel() {
  const [signal, setSignal] = useState<HubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubSignal(getTodayFolder())
      .then((s) => {
        if (mounted) setSignal(s);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading && !signal) {
    return (
      <div className="rounded-lg border bg-white/50 p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-2 h-4 w-24 animate-pulse rounded bg-gray-200" />
      </div>
    );
  }

  if (!signal) return null;

  return (
    <div className="rounded-lg border bg-gradient-to-br from-blue-50 to-indigo-50 p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Top Hub Signal</p>
          <p className="mt-1 text-lg font-semibold text-gray-900">{signal.hub}</p>
          <p className="text-xs text-gray-600">{signal.label}</p>
        </div>
        <div className="text-right">
          <span className="inline-flex items-center rounded-full bg-white/60 px-2.5 py-0.5 text-xs font-medium text-gray-700">
            {Math.round(signal.score * 100)}%
          </span>
          <p className="mt-1 text-xs text-gray-500">
            updated {formatDistanceToNow(new Date(signal.updated_at), { addSuffix: true })}
          </p>
        </div>
      </div>

      <div className="mt-3 space-y-1.5">
        {signal.insights.map((insight, i) => (
          <p key={i} className="text-sm text-gray-700">• {insight}</p>
        ))}
      </div>

      {signal.links && signal.links.length > 0 && (
        <div className="mt-3 flex gap-2">
          {signal.links.map((link, i) => (
            <a
              key={i}
              href={link.href}
              className="text-xs font-medium text-indigo-600 hover:underline"
            >
              {link.label}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
```

### 4) Mount in Dashboard (`src/pages/Dashboard.tsx`)
Locate the main dashboard layout and add the panel in a prominent but non-blocking area (e.g., sidebar header or top-row aside). Example placement:

```tsx
// src/pages/Dashboard.tsx
import TopHubPanel from '../components/TopHubPanel';

export default function Dashboard() {
  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      {/* Sidebar */}
      <aside className="w-full lg:w-80 border-b lg:border-r bg-white/70 p-4 lg:p-6">
        <TopHubPanel />
        {/* ...rest of sidebar nav */}
      </aside>

      {/* Main content */}
      <main className="flex-1 p-
