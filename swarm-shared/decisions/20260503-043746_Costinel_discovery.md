# Costinel / discovery

**Final Synthesized Plan — CDN-First Top-Hub Signal Panel (<2h, resilient, read-only)**

**Goal**  
Add a resilient “Top Hub” signal panel to Costinel that shows the most-connected hub (e.g., “MOC”) and related docs using **CDN-fetched artifacts only** (zero runtime HF API calls). Strict **Sense + Signal — No Execute** (read-only, no mutations, no client secrets).

---

### Architecture Decisions (resolved)
- **CDN-only at runtime**: No Hugging Face client calls in the browser.  
- **Pre-listed manifest at build time** (optional but recommended): A Mac/CI step saves `top-hub-manifest.json` into repo so the client never lists files.  
- **Fail-safe UI**: Graceful empty state on CDN failure (no crash, no retry loops in UI).  
- **Caching & resilience**: CDN utility uses timeout, exponential backoff, and treats 404 as “missing” (no retry).  
- **No execute**: Panel is strictly read-only.

---

### File Changes (4 files, <2h)

1. `src/lib/cdn.ts` — robust CDN fetcher  
2. `src/hooks/useTopHubSignal.ts` — hook with loading/error/idempotent behavior  
3. `src/components/TopHubSignalPanel.tsx` — presentational card  
4. `src/pages/Dashboard.tsx` — non-breaking integration

---

### 1) `src/lib/cdn.ts`
```ts
// src/lib/cdn.ts
const DEFAULT_TIMEOUT = 8_000;
const MAX_RETRIES = 2;

export interface CDNOptions {
  repo: string; // e.g. "AXENTX/Signals"
  timeout?: number;
}

async function fetchWithTimeout(url: string, options: RequestInit & { timeout?: number } = {}) {
  const { timeout = DEFAULT_TIMEOUT, ...init } = options;
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(url, { ...init, signal: controller.signal, cache: 'no-store' });
    return res;
  } finally {
    clearTimeout(id);
  }
}

export async function fetchFromCDN<T = unknown>(path: string, opts: CDNOptions): Promise<T | null> {
  const { repo, timeout } = opts;
  const url = `https://huggingface.co/datasets/${repo}/resolve/main/${path.replace(/^\/+/, '')}`;
  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      // small exponential-ish backoff
      await new Promise((r) => setTimeout(r, 300 * attempt));
    }
    try {
      const res = await fetchWithTimeout(url, { timeout });
      if (res.ok) return (await res.json()) as T;
      // 404 -> treat as missing (no retry)
      if (res.status === 404) return null;
      // 429/5xx -> retry
      lastError = new Error(`CDN fetch ${res.status} ${res.statusText}`);
    } catch (err: any) {
      lastError = err;
      // network/abort -> retry
    }
  }
  // non-blocking warning
  // eslint-disable-next-line no-console
  console.warn('[cdn] failed to fetch', url, lastError);
  return null;
}
```

---

### 2) `src/hooks/useTopHubSignal.ts`
```ts
// src/hooks/useTopHubSignal.ts
import { useEffect, useState, useCallback } from 'react';
import { fetchFromCDN } from '../lib/cdn';

export interface RelatedDoc {
  title: string;
  slug: string;
  url?: string;
  snippet?: string;
}

export interface TopHubPayload {
  hub: string;
  insight: string;
  period?: string;
  related: RelatedDoc[];
  generatedAt?: string;
}

interface UseTopHubSignalOptions {
  repo?: string;
  dateFolder?: string; // e.g. "2026-04-27"
  enabled?: boolean;
}

export function useTopHubSignal(opts: UseTopHubSignalOptions = {}) {
  const { repo = 'AXENTX/Signals', dateFolder, enabled = true } = opts;
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(!!enabled);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    let mounted = true;
    setLoading(true);
    setError(null);

    try {
      const basePath = dateFolder
        ? `signals/top-hub/${dateFolder}/top-hub.json`
        : 'signals/top-hub/latest/top-hub.json';
      const payload = await fetchFromCDN<TopHubPayload>(basePath, { repo });

      if (mounted) {
        if (payload) {
          setData(payload);
        } else {
          setError('No signal available');
          setData(null);
        }
      }
    } catch (err: any) {
      if (mounted) {
        setError(err?.message || 'Failed to load signal');
        setData(null);
      }
    } finally {
      if (mounted) setLoading(false);
    }
  }, [repo, dateFolder, enabled]);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}
```

---

### 3) `src/components/TopHubSignalPanel.tsx`
```tsx
// src/components/TopHubSignalPanel.tsx
import React from 'react';
import { useTopHubSignal, RelatedDoc } from '../hooks/useTopHubSignal';

const RelatedDocItem: React.FC<{ doc: RelatedDoc }> = ({ doc }) => {
  const target = doc.url ? '_blank' : undefined;
  const href = doc.url || `#${doc.slug}`;
  return (
    <li className="mb-2">
      <a
        href={href}
        target={target}
        rel={target === '_blank' ? 'noopener noreferrer' : undefined}
        className="text-sm text-blue-600 hover:underline"
      >
        {doc.title || doc.slug}
      </a>
      {doc.snippet && <p className="text-xs text-gray-600 mt-0.5">{doc.snippet}</p>}
    </li>
  );
};

const TopHubSignalPanel: React.FC<{ repo?: string; dateFolder?: string }> = ({ repo, dateFolder }) => {
  const { data, loading, error } = useTopHubSignal({ repo, dateFolder, enabled: true });

  if (loading) {
    return (
      <div className="p-4 border rounded bg-gray-50">
        <p className="text-sm text-gray-500">Loading top hub signal…</p>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-4 border rounded bg-gray-50">
        <p className="text-sm text-gray-500">Signal unavailable.</p>
      </div>
    );
  }

  return (
    <div className="p-4 border rounded bg-white shadow-sm">
      <h2 className="text-lg font-semibold text-gray-900">Top Hub: {data.hub}</h2>
      {data.period && <p className="text-xs text-gray-400 mb-2">{data.period}</p>}
      <p className="text-sm text-gray-700 mb-3">{data.insight}</p>

      {data.related && data.related.length > 0 && (
        <>
          <h3 className="text-sm font-medium text-gray-800 mb-2">Related Docs</h3>
          <ul className="list-none pl-0 m-0">
            {data.related.map((doc,
