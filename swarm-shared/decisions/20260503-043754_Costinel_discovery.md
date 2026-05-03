# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Ship a resilient “Top Hub” signal panel into Costinel that shows the most-connected hub (e.g., “MOC”) and related docs using CDN-fetched artifacts (zero runtime HF API calls).  
**Principle**: Strict **Sense + Signal (ไม่ Execute)** — panel proposes signals for human review; no automated changes or mutations.

---

### Scope (incremental, <2h)
- Add `TopHubSignalPanel` component + `useTopHubSignal` hook + `cdn.ts` helper.
- Fetch `top-hub.json` and `related-docs.json` from CDN (public dataset path).
- Render panel on Dashboard with graceful fallback, last-updated timestamp, and retry.
- No mutations, no background jobs, no HF API during runtime.
- TypeScript strict mode passes; lightweight smoke test to verify render.

### File layout
```
src/
  components/
    TopHubSignalPanel.tsx      (new)
  hooks/
    useTopHubSignal.ts         (new)
  lib/
    cdn.ts                     (new)
  pages/
    Dashboard.tsx              (modify)
```

---

### 1) CDN helper (`src/lib/cdn.ts`)
Single responsibility: fetch pre-listed artifact JSON via CDN (no auth, no API rate limit).

```ts
// src/lib/cdn.ts
const REPO = 'AXENTX/knowledge-rag';
const BASE = `https://huggingface.co/datasets/${REPO}/resolve/main`;

export type TopHub = {
  hub_id: string;
  label: string;
  degree: number;
  description: string;
  updated_at: string; // ISO
};

export type RelatedDoc = {
  doc_id: string;
  title: string;
  summary: string;
  url?: string;
  relevance_score: number;
  tags?: string[];
};

export type TopHubPayload = {
  top_hub: TopHub;
  related_docs: RelatedDoc[];
  fetched_at: string;
};

export async function fetchTopHubSignal(): Promise<TopHubPayload | null> {
  try {
    const r = await fetch(`${BASE}/costinel/top-hub.json?cacheBust=${Date.now()}`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
    });
    if (!r.ok) throw new Error(`CDN fetch failed: ${r.status}`);
    return (await r.json()) as TopHubPayload;
  } catch (err) {
    console.error('[cdn] fetchTopHubSignal failed', err);
    return null;
  }
}
```

---

### 2) Hook (`src/hooks/useTopHubSignal.ts`)
Fetch + memoize + error boundary; expose `{ data, loading, error, refetch }`.

```ts
// src/hooks/useTopHubSignal.ts
import { useEffect, useState, useCallback } from 'react';
import { fetchTopHubSignal, TopHubPayload } from '../lib/cdn';

const POLL_INTERVAL = 5 * 60 * 1000; // 5m

export function useTopHubSignal(enabled = true) {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const payload = await fetchTopHubSignal();
      if (payload) {
        setData(payload);
        setError(null);
      } else {
        setError('Unable to load top-hub signal');
      }
    } catch (err: any) {
      setError(err?.message || 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    load();
    if (!enabled) return;
    const id = setInterval(load, POLL_INTERVAL);
    return () => clearInterval(id);
  }, [load, enabled]);

  return { data, loading, error, refetch: load };
}
```

---

### 3) Component (`src/components/TopHubSignalPanel.tsx`)
Present hub card + related docs list; actionable links open in new tab (no execute). Includes graceful states and retry.

```tsx
// src/components/TopHubSignalPanel.tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import type { TopHub, RelatedDoc } from '../lib/cdn';

export function TopHubSignalPanel() {
  const { data, loading, error, refetch } = useTopHubSignal(true);

  if (loading && !data) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">Loading top-hub signal…</p>
      </div>
    );
  }

  if (error && !data) {
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

  if (!data?.top_hub) return null;

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h3 className="text-base font-semibold">Top Hub Signal</h3>
          <p className="text-xs text-muted-foreground">
            Most-connected hub — Sense + Signal (ไม่ Execute)
          </p>
        </div>
        <span className="text-xs text-muted-foreground">
          Updated {formatTime(data.top_hub.updated_at)}
        </span>
      </div>

      <HubCard hub={data.top_hub} />

      {data.related_docs && data.related_docs.length > 0 && (
        <div className="mt-4">
          <h4 className="mb-2 text-sm font-medium">Related Docs</h4>
          <ul className="space-y-2" role="list">
            {data.related_docs.map((doc) => (
              <li key={doc.doc_id} className="text-sm">
                <a
                  href={doc.url || '#'}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:underline"
                >
                  {doc.title}
                </a>
                <p className="text-xs text-muted-foreground">{doc.summary}</p>
                {doc.tags && doc.tags.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {doc.tags.map((t) => (
                      <span
                        key={t}
                        className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 flex items-center justify-end gap-2 text-xs text-muted-foreground">
        <span>Signal generated at {formatTime(data.fetched_at)}</span>
        <button
          onClick={() => refetch()}
          className="text-xs text-blue-600 underline hover:text-blue-800"
        >
          Refresh
        </button>
      </div>
    </div>
  );
}

function HubCard({ hub }: { hub: TopHub }) {
  return (
    <div className="rounded-md border bg-muted/50 p-3">
      <div className="flex items-start justify-between">
        <div>
          <p className="font-semibold">{hub.label}</p>
          <p className="text-xs text-muted-foreground">{hub.hub_id}</p>
        </div>
        <span className="rounded bg
