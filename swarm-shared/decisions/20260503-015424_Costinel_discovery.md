# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- Ships in <2h as a resilient, self-contained widget that degrades gracefully when graph data is unavailable.  
- Aligns with past pattern: review most-connected hub before planning (#knowledge-rag #graph #hub).

---

### 1) Implementation Steps (ordered)

1. **Locate dashboard layout**  
   Identify the main dashboard file (likely `pages/dashboard.tsx` or `components/Dashboard.tsx`). Add a new panel region `TopHubSignals` adjacent to existing cost widgets.

2. **Create `TopHubSignals` component**  
   - Accepts `hubKey?: string` (default `"MOC"`).  
   - Fetches `/api/knowledge-graph/hubs/{hubKey}/signals` (or local JSON fallback).  
   - Renders: hub title, short description, list of proposals with `title`, `impact`, `effort`, `actions` (links), and `auditTrailId`.  
   - Empty state: “No active signals — run discovery to populate.”

3. **Add lightweight API route (or mock)**  
   - If backend exists: `pages/api/knowledge-graph/hubs/[hubKey]/signals.ts` returning `{ hub, signals: Array<{id, title, impact, effort, context, proposalUrl}> }`.  
   - If no backend: embed a static JSON at `public/data/top-hub-signals.json` and fetch it (CDN bypass pattern) to avoid API rate limits during local dev.

4. **Integrate into dashboard**  
   - Place component in the top row or sidebar of the dashboard for high visibility.  
   - Add collapsible behavior for small screens.

5. **Styling & accessibility**  
   - Use existing design tokens (colors, spacing).  
   - Each signal card has `role="article"` and keyboard focus.  
   - Links open in new tab with `rel="noopener noreferrer"`.

6. **Resilience & observability**  
   - Wrap fetch in try/catch; fallback to static JSON on error.  
   - Log non-blocking errors to console (do not crash UI).  
   - Add lightweight telemetry event `top_hub_signal_impression` for future iteration.

7. **Tests & validation**  
   - Smoke test: load dashboard, verify panel renders.  
   - Verify graceful degradation when endpoint 404s.

---

### 2) Code Snippets

#### `components/TopHubSignals.tsx`

```tsx
'use client';

import { useEffect, useState } from 'react';

type Signal = {
  id: string;
  title: string;
  impact: 'high' | 'medium' | 'low';
  effort: 'low' | 'medium' | 'high';
  context: string;
  proposalUrl?: string;
  auditTrailId?: string;
};

type HubData = {
  hubKey: string;
  name: string;
  description: string;
  signals: Signal[];
};

const FALLBACK_URL = '/data/top-hub-signals.json';

export default function TopHubSignals({ hubKey = 'MOC' }: { hubKey?: string }) {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        // Prefer API; fallback to static CDN asset (bypasses API auth/rate-limits)
        const res = await fetch(`/api/knowledge-graph/hubs/${hubKey}/signals`, {
          cache: 'no-store',
        }).catch(() => null);

        if (res?.ok) {
          const data = await res.json();
          setHub(data);
        } else {
          const fallback = await fetch(FALLBACK_URL, { cache: 'default' });
          if (!fallback.ok) throw new Error('No signals available');
          const data = await fallback.json();
          setHub(data);
        }
      } catch (err) {
        setError(String(err));
        console.error('[TopHubSignals]', err);
      } finally {
        setLoading(false);
      }
    }

    load();
  }, [hubKey]);

  if (loading) {
    return (
      <section aria-busy="true" className="p-4 rounded border border-gray-200">
        <h2 className="text-lg font-semibold mb-2">Loading signals…</h2>
      </section>
    );
  }

  if (error || !hub) {
    return (
      <section className="p-4 rounded border border-gray-200">
        <h2 className="text-lg font-semibold mb-2">Top hub signals</h2>
        <p className="text-sm text-gray-600">
          No active signals available. Run discovery to populate insights.
        </p>
      </section>
    );
  }

  const impactColor = (imp: Signal['impact']) => {
    switch (imp) {
      case 'high':
        return 'text-red-700 bg-red-50 border border-red-200';
      case 'medium':
        return 'text-amber-700 bg-amber-50 border border-amber-200';
      default:
        return 'text-green-700 bg-green-50 border border-green-200';
    }
  };

  return (
    <section aria-labelledby="hub-title" className="p-4 rounded border border-gray-200">
      <header className="mb-3">
        <h2 id="hub-title" className="text-lg font-semibold">
          {hub.name}
        </h2>
        <p className="text-sm text-gray-600">{hub.description}</p>
      </header>

      <ul className="space-y-3" role="list">
        {hub.signals.map((s) => (
          <li
            key={s.id}
            role="article"
            aria-label={`Signal: ${s.title}`}
            className="p-3 rounded border border-gray-100 bg-white"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <h3 className="font-medium text-sm truncate">{s.title}</h3>
                <p className="text-xs text-gray-500 mt-1 line-clamp-2">{s.context}</p>
                <div className="flex items-center gap-2 mt-2 text-xs">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${impactColor(s.impact)}`}>
                    {s.impact} impact
                  </span>
                  <span className="text-gray-400">·</span>
                  <span className="text-gray-500">effort: {s.effort}</span>
                </div>
              </div>

              {s.proposalUrl && (
                <a
                  href={s.proposalUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-600 hover:underline whitespace-nowrap"
                >
                  View proposal
                </a>
              )}
            </div>

            {s.auditTrailId && (
              <p className="mt-2 text-xs text-gray-400">Audit: {s.auditTrailId}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
```

#### `pages/api/knowledge-graph/hubs/[hubKey]/signals.ts` (optional backend shim)

```ts
import type { NextApiRequest, NextApiResponse } from 'next';

// Lightweight shim. Replace with real graph query when available.
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  const { hubKey } = req.query;

  // Basic allowlist to avoid open redirects or excessive queries
  const allowed = ['MOC', 'COST
