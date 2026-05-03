# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cache-friendly, and deployable in <2h.

---

### 1) Architecture (CDN-first)

- **Data source**: Public CDN file  
  `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/current.json`
- **Schema** (minimal):
  ```json
  {
    "hub": "MOC",
    "score": 0.94,
    "label": "Most-connected hub",
    "updated": "2026-05-03T04:00:00Z",
    "url": "https://huggingface.co/datasets/axentx/knowledge-rag/blob/main/hubs/MOC.md"
  }
  ```
- **Delivery**: Static JSON via CDN (no Authorization, no HF API calls at runtime).
- **Caching**: `Cache-Control: public, max-age=300` (5m) + `stale-while-revalidate` in UI.
- **Fallback**: Local fallback snapshot bundled at build time (`public/top-hub-fallback.json`) so UI never blanks.

---

### 2) File Changes

```
/opt/axentx/Costinel/
├── public/
│   ├── top-hub-fallback.json        # committed fallback
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.tsx    # new
│   ├── hooks/
│   │   └── useTopHubSignal.ts       # new
│   ├── lib/
│   │   └── config.ts                # add CDN_URL
│   └── App.tsx (or dashboard layout) # import panel
```

---

### 3) Code Snippets

#### `public/top-hub-fallback.json`
```json
{
  "hub": "MOC",
  "score": 0.94,
  "label": "Most-connected hub",
  "updated": "2026-05-03T04:00:00Z",
  "url": "https://huggingface.co/datasets/axentx/knowledge-rag/blob/main/hubs/MOC.md"
}
```

#### `src/lib/config.ts`
```ts
export const TOP_HUB_CDN_URL =
  'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/current.json';
```

#### `src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import { TOP_HUB_CDN_URL } from '../lib/config';

export interface TopHubSignal {
  hub: string;
  score: number;
  label: string;
  updated: string;
  url: string;
}

const FALLBACK_URL = '/top-hub-fallback.json';
const CACHE_KEY = 'costinel:top-hub:cached';
const CACHE_TS_KEY = 'costinel:top-hub:ts';
const TTL_MS = 5 * 60 * 1000; // 5m

async function fetchWithTimeout(url: string, timeout = 4000): Promise<Response> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    return await fetch(url, { signal: controller.signal, cache: 'no-store' });
  } finally {
    clearTimeout(id);
  }
}

export function useTopHubSignal(enabled = true) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);

  const loadFallback = useCallback(async (): Promise<TopHubSignal> => {
    const res = await fetch(FALLBACK_URL, { cache: 'force-cache' });
    if (!res.ok) throw new Error('Fallback unavailable');
    return res.json();
  }, []);

  const fetchFresh = useCallback(async (): Promise<TopHubSignal> => {
    const res = await fetchWithTimeout(TOP_HUB_CDN_URL, 4000);
    if (!res.ok) throw new Error(`CDN responded ${res.status}`);
    return res.json();
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    // Try cache first (fast)
    const cachedRaw = localStorage.getItem(CACHE_KEY);
    const tsRaw = localStorage.getItem(CACHE_TS_KEY);
    if (cachedRaw && tsRaw) {
      const ts = Number(tsRaw);
      if (Date.now() - ts < TTL_MS) {
        try {
          setData(JSON.parse(cachedRaw));
          setLoading(false);
          // background refresh
          fetchFresh()
            .then((fresh) => {
              localStorage.setItem(CACHE_KEY, JSON.stringify(fresh));
              localStorage.setItem(CACHE_TS_KEY, String(Date.now()));
              setData(fresh);
            })
            .catch(() => {});
          return;
        } catch {
          // ignore corrupted cache
        }
      }
    }

    // No valid cache: try CDN, fallback to bundled
    try {
      const fresh = await fetchFresh();
      localStorage.setItem(CACHE_KEY, JSON.stringify(fresh));
      localStorage.setItem(CACHE_TS_KEY, String(Date.now()));
      setData(fresh);
    } catch (err) {
      try {
        const fallback = await loadFallback();
        setData(fallback);
      } catch (err2) {
        setError(err2 instanceof Error ? err2 : new Error(String(err2)));
      }
    } finally {
      setLoading(false);
    }
  }, [enabled, fetchFresh, loadFallback]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { data, loading, error, refresh };
}
```

#### `src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import { ExternalLink } from 'lucide-react';

export function TopHubSignalPanel() {
  const { data, loading, error } = useTopHubSignal(true);

  if (loading && !data) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-4 py-2 text-sm text-gray-500">
        Loading top hub signal...
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="rounded-lg border border-yellow-100 bg-yellow-50 px-4 py-2 text-sm text-yellow-700">
        Signal unavailable
      </div>
    );
  }

  if (!data) return null;

  return (
    <a
      href={data.url}
      target="_blank"
      rel="noopener noreferrer"
      className="group block rounded-lg border border-emerald-100 bg-emerald-50/60 px-4 py-2 transition hover:bg-emerald-50"
      title={`Updated ${new Date(data.updated).toLocaleString()}`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-emerald-800">{data.hub}</span>
          <span className="text-xs text-emerald-600">{data.label}</span>
        </div>
        <div className="flex items-center gap-1 text-xs text-emerald-600 opacity-60 group-hover:opacity-100">
          <span>score</span>
          <span className="font-mono font-medium">{(data.score * 100).toFixed(0)}</span>
          <ExternalLink className="h-3 w-3
