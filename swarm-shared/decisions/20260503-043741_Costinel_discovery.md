# Costinel / discovery

**Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)**

**Goal**: Add a resilient “Top Hub” signal panel to Costinel that surfaces the most-connected hub (e.g., “MOC”) and related docs using CDN-fetched artifacts (zero runtime HF API calls).  
**Principle**: Strict **Sense + Signal — No Execute**.

---

### 1) CDN utility (`src/lib/cdn.ts`)
Lightweight, no-auth CDN fetcher with timeout, retry, and memory cache. Uses a build-time static fallback for offline resilience.

```ts
// src/lib/cdn.ts
const CDN_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5m
const FETCH_TIMEOUT_MS = 8_000;
const RETRIES = 2;

type CacheEntry<T> = { data: T; ts: number };
const memoryCache = new Map<string, CacheEntry<unknown>>();

export interface TopHubSignal {
  hub: string;
  score: number;
  relatedDocs: Array<{
    slug: string;
    title: string;
    summary: string;
    score: number;
  }>;
  generatedAt: string; // ISO
}

function isCacheValid(ts: number) {
  return Date.now() - ts < CACHE_TTL_MS;
}

async function fetchWithTimeout<T>(url: string, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const combinedSignal =
    signal && controller.signal !== signal
      ? // If external signal provided, race both
        (() => {
          const race = new AbortController();
          const onAbort = () => race.abort();
          signal.addEventListener('abort', onAbort, { once: true });
          controller.signal.addEventListener('abort', () => {
            signal.removeEventListener('abort', onAbort);
          }, { once: true });
          return race.signal;
        })()
      : controller.signal;

  const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, { cache: 'no-store', signal: combinedSignal });
    clearTimeout(timeoutId);
    if (!res.ok) {
      const err: Error & { status?: number } = new Error(`CDN fetch failed: ${res.status} ${url}`);
      err.status = res.status;
      throw err;
    }
    return (await res.json()) as T;
  } catch (err) {
    clearTimeout(timeoutId);
    throw err;
  }
}

async function fetchJson<T>(path: string, attempt = 0): Promise<T> {
  const cached = memoryCache.get(path) as CacheEntry<T> | undefined;
  if (cached && isCacheValid(cached.ts)) return cached.data;

  const url = `${CDN_BASE}/${path}`;
  try {
    const data = await fetchWithTimeout<T>(url);
    memoryCache.set(path, { data, ts: Date.now() });
    return data;
  } catch (err: any) {
    if (attempt < RETRIES) return fetchJson<T>(path, attempt + 1);
    // Distinguish 404 for graceful fallback
    const status = err?.status;
    const e: Error & { status?: number } = new Error(`CDN fetch failed: ${err?.message || err}`);
    e.status = status;
    throw e;
  }
}

// Fallback loader (build-time static file bundled in public/)
async function loadFallback<T>(fallbackPath: string): Promise<T | null> {
  try {
    const res = await fetch(fallbackPath, { cache: 'default' });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchTopHubSignal(dateFolder = 'latest'): Promise<TopHubSignal | null> {
  try {
    const result = await fetchJson<TopHubSignal>(`top-hub/${dateFolder}/signal.json`);
    return result;
  } catch (err: any) {
    if (err?.status === 404) {
      // Try build-time fallback
      const fallback = await loadFallback<TopHubSignal>('/fallback/top-hub.json');
      if (fallback) return fallback;
      return null;
    }
    // Non-404 network/server errors: log but don't break UI
    console.warn('[cdn] top-hub signal fetch failed', err);
    // Try fallback for transient failures too
    const fallback = await loadFallback<TopHubSignal>('/fallback/top-hub.json');
    return fallback ?? null;
  }
}
```

---

### 2) Hook (`src/hooks/useTopHubSignal.ts`)
Polling + manual refetch, resilient to failures, exposes loading/error/state.

```ts
// src/hooks/useTopHubSignal.ts
import { useEffect, useState, useCallback } from 'react';
import { fetchTopHubSignal, TopHubSignal } from '../lib/cdn';

export function useTopHubSignal(dateFolder = 'latest', pollIntervalMs = 120_000) {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchTopHubSignal(dateFolder);
      setSignal(result);
    } catch (err: any) {
      setError(err?.message || 'Unknown error');
      setSignal(null);
    } finally {
      setLoading(false);
    }
  }, [dateFolder]);

  useEffect(() => {
    load();
    const id = setInterval(load, pollIntervalMs);
    return () => clearInterval(id);
  }, [load, pollIntervalMs]);

  return { signal, loading, error, refetch: load };
}
```

---

### 3) Component (`src/components/TopHubSignalPanel.tsx`)
Dashboard-ready card with clear states, refresh control, and “Sense + Signal” notice.

```tsx
// src/components/TopHubSignalPanel.tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';

export function TopHubSignalPanel({ dateFolder = 'latest' }: { dateFolder?: string }) {
  const { signal, loading, error, refetch } = useTopHubSignal(dateFolder, 120_000);

  if (loading && !signal) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">Loading top-hub signal…</p>
      </div>
    );
  }

  if (error && !signal) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-red-600">{error}</p>
        <button
          onClick={() => refetch()}
          className="mt-2 text-sm text-blue-600 underline hover:text-blue-800"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!signal) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">No top-hub signal available.</p>
        <button
          onClick={() => refetch()}
          className="mt-2 text-sm text-blue-600 underline hover:text-blue-800"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-lg font-semibold">Top Hub Signal</h3>
          <p className="text-sm text-muted-foreground">
            Most-connected hub: <span className="font-mono font-medium">{signal.hub}</span>
          </p>
          <p className="text-xs text-muted-foreground">
            Score: {signal.score
