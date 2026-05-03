# Costinel / frontend

## Final Implementation Plan — Top Hub Signal Panel (CDN-first)

**Scope**: Frontend-only addition to Costinel dashboard.  
**Effort**: ~60–90 minutes.  
**Mechanism**: CDN JSON fetch (no auth, no backend) with local fallback and client cache.  
**Goal**: Show the most-connected hub, a concise insight, and top related docs; resilient to CDN failures.

---

### 1) Data contract (CDN JSON)

Path (example):  
`https://hagingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub/latest.json`

```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "score": 0.94,
  "rank": 1,
  "insight": "Most-connected hub for cost governance playbooks; central to anomaly triage and proposal handoffs.",
  "related": [
    {
      "id": "ri-coverage-2026",
      "title": "RI Coverage Analysis",
      "snippet": "Increase reserved coverage in us-east-1",
      "score": 0.87,
      "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/..."
    },
    {
      "id": "anomaly-egress",
      "title": "Egress Spike Detection",
      "snippet": "Unusual cross-region egress on 2026-04-28",
      "score": 0.81,
      "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/..."
    }
  ],
  "updatedAt": "2026-04-27T14:30:00.000Z",
  "sourceUrl": "https://huggingface.co/datasets/axentx/costinel-knowledge/blob/main/top-hub/latest.json"
}
```

Notes:
- `hub` (string) is required.  
- `related` is an array; each item should include `id`, `title`, and at least one of `snippet` or `url`.  
- `score`/`rank` optional but encouraged.  
- `insight` is a short human-readable summary.

---

### 2) File layout

- `public/data/top-hub.json` — committed local snapshot (fallback).  
- `src/components/TopHubSignalPanel.tsx` — new component (TypeScript).  
- `src/hooks/useTopHubSignal.ts` — hook for CDN fetch, cache, and fallback logic.  
- `src/pages/Dashboard.tsx` — mount the panel in the dashboard grid near the top.

---

### 3) Hook: `useTopHubSignal.ts`

```ts
import { useEffect, useState, useCallback } from 'react';

const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub/latest.json';
const FALLBACK_URL = '/data/top-hub.json';
const CACHE_KEY = 'costinel:top-hub';
const CACHE_TTL_MS = 10 * 60 * 1000; // 10 minutes

export interface RelatedDoc {
  id: string;
  title: string;
  snippet?: string;
  score?: number;
  url?: string;
}

export interface TopHubPayload {
  hub: string;
  label?: string;
  score?: number;
  rank?: number;
  insight?: string;
  related: RelatedDoc[];
  updatedAt?: string;
  sourceUrl?: string;
}

function isPayload(obj: any): obj is TopHubPayload {
  return obj && typeof obj.hub === 'string' && Array.isArray(obj.related);
}

function fetchWithTimeout(url: string, timeoutMs: number): Promise<any> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { signal: controller.signal })
    .then((res) => {
      clearTimeout(id);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .finally(() => clearTimeout(id));
}

export default function useTopHubSignal() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (isBackground = false) => {
    if (!isBackground) setLoading(true);
    try {
      const json = await fetchWithTimeout(CDN_URL, 3000);
      if (isPayload(json)) {
        setData(json);
        localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), payload: json }));
        setError(null);
        return;
      }
      throw new Error('Invalid CDN payload');
    } catch {
      try {
        const json = await fetchWithTimeout(FALLBACK_URL, 2000);
        if (isPayload(json)) {
          setData(json);
          setError(null);
          return;
        }
        throw new Error('Invalid fallback payload');
      } catch {
        if (!isBackground) setError('Unable to load top-hub signal');
      }
    } finally {
      if (!isBackground) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const cached = localStorage.getItem(CACHE_KEY);
    const now = Date.now();

    // Use valid cache immediately
    if (cached) {
      try {
        const parsed = JSON.parse(cached);
        if (parsed?.ts && now - parsed.ts < CACHE_TTL_MS && isPayload(parsed.payload)) {
          setData(parsed.payload);
          setLoading(false);
        }
      } catch {
        // ignore malformed cache
      }
    }

    // Always attempt fresh fetch (background if we have cached data)
    const isBackground = !!data;
    load(isBackground).catch(() => {});
  }, [load, data]);

  const retry = () => load(false);

  return { data, loading, error, retry };
}
```

---

### 4) Component: `TopHubSignalPanel.tsx`

```tsx
import React from 'react';
import useTopHubSignal, { TopHubPayload } from '../hooks/useTopHubSignal';

const RelatedPill: React.FC<{
  doc: TopHubPayload['related'][0];
  onClick?: () => void;
}> = ({ doc, onClick }) => (
  <button
    onClick={onClick}
    className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200 hover:bg-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-500"
    title={doc.snippet}
  >
    <span className="truncate">{doc.title}</span>
    {typeof doc.score === 'number' && (
      <span className="shrink-0 text-[10px] tabular-nums text-emerald-500">
        {Math.round(doc.score * 100)}%
      </span>
    )}
  </button>
);

export default function TopHubSignalPanel() {
  const { data, loading, error, retry } = useTopHubSignal();

  // Loading (no cached data)
  if (loading && !data) {
    return (
      <div className="rounded-lg border border-gray-100 bg-white p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-3 flex flex-wrap gap-2">
          <div className="h-6 w-20 animate-pulse rounded-full bg-gray-100" />
          <div className="h-6 w-24 animate-pulse rounded-full bg-gray-100" />
          <div className="h-6 w-28 animate-pulse rounded-full bg-gray-100" />
        </div>
      </div>
    );
  }

  // Error with no data
