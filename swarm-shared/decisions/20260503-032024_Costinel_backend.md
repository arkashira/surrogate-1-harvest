# Costinel / backend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected hub (default `MOC`) from a lightweight graph index
- Uses CDN-first data fetching (bypasses HF API rate limits) for panel assets/context
- Renders in <100ms, fails gracefully, never blocks Costinel core flows
- Follows pattern: review top-hub before planning; tags `#knowledge-rag #graph #hub`

---

### 1) File changes (3 files, ~120 lines total)

#### A) Backend: `/opt/axentx/Costinel/src/services/topHubService.ts`
Lightweight service that resolves the top hub and produces a signal payload. Uses local graph index; CDN fallback for remote context.

```ts
// src/services/topHubService.ts
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';

const GRAPH_INDEX_PATH = join(process.cwd(), 'data', 'graph-index.json');
const CDN_CONTEXT_URL = 'https://huggingface.co/datasets/axentx/top-hub-context/resolve/main/latest.json';

export interface TopHubSignal {
  hubId: string;
  label: string;
  rank: number;
  connections: number;
  lastUpdated: string;
  context?: {
    summary: string;
    actionItems: string[];
    docs: Array<{ title: string; url: string }>;
  };
  source: 'local' | 'cdn' | 'default';
}

function defaultSignal(): TopHubSignal {
  return {
    hubId: 'MOC',
    label: 'MOC',
    rank: 1,
    connections: 0,
    lastUpdated: new Date().toISOString(),
    source: 'default',
  };
}

export async function getTopHubSignal(): Promise<TopHubSignal> {
  try {
    // 1) Try local graph index (fast, zero network)
    if (existsSync(GRAPH_INDEX_PATH)) {
      const raw = readFileSync(GRAPH_INDEX_PATH, 'utf8');
      const index = JSON.parse(raw);
      const top = index.hubs?.sort((a: any, b: any) => b.connections - a.connections)[0];
      if (top) {
        return {
          hubId: top.id,
          label: top.label || top.id,
          rank: 1,
          connections: top.connections || 0,
          lastUpdated: index.generatedAt || new Date().toISOString(),
          source: 'local',
        };
      }
    }

    // 2) CDN fallback (bypasses HF API auth/rate limits)
    const res = await fetch(CDN_CONTEXT_URL, { method: 'GET', cache: 'no-store' });
    if (res.ok) {
      const cdn = await res.json();
      return {
        hubId: cdn.hubId || 'MOC',
        label: cdn.label || cdn.hubId || 'MOC',
        rank: cdn.rank || 1,
        connections: cdn.connections || 0,
        lastUpdated: cdn.lastUpdated || new Date().toISOString(),
        context: cdn.context,
        source: 'cdn',
      };
    }

    // 3) Safe default
    return defaultSignal();
  } catch (err) {
    // Never throw — panel must not block dashboard
    console.warn('[TopHubSignal] failed, using default', err);
    return defaultSignal();
  }
}
```

#### B) API route: `/opt/axentx/Costinel/src/routes/api/top-hub.ts`
Expose a fast, cached endpoint for the frontend panel.

```ts
// src/routes/api/top-hub.ts
import { Router } from 'express';
import { getTopHubSignal } from '../../services/topHubService';

const router = Router();

router.get('/top-hub', async (req, res) => {
  try {
    const signal = await getTopHubSignal();
    // Short cache: 60s for CDN/local; keeps panel fresh without load
    res.set('Cache-Control', 'public, max-age=60, stale-while-revalidate=30');
    res.json({ ok: true, data: signal });
  } catch (err) {
    res.status(500).json({ ok: false, error: 'Unable to load top-hub signal' });
  }
});

export default router;
```

Wire this route into your main app (e.g., in `src/app.ts` or `src/server.ts`):
```ts
import topHubRoute from './routes/api/top-hub';
app.use('/api', topHubRoute);
```

#### C) Frontend panel: `/opt/axentx/Costinel/src/components/TopHubSignalPanel.tsx`
Non-blocking React panel that fetches and renders signal; skeleton-first, graceful fallback.

```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface Context {
  summary: string;
  actionItems: string[];
  docs: Array<{ title: string; url: string }>;
}

interface Signal {
  hubId: string;
  label: string;
  rank: number;
  connections: number;
  lastUpdated: string;
  context?: Context;
  source: string;
}

export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState<Signal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/api/top-hub')
      .then((r) => r.json())
      .then((json) => {
        if (mounted && json?.data) setSignal(json.data);
      })
      .catch(() => {
        // silent fail — panel remains empty
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
      <div className="top-hub-panel skeleton" aria-hidden="true">
        <div className="sh-title"></div>
        <div className="sh-meta"></div>
      </div>
    );
  }

  if (!signal) return null;

  return (
    <div className="top-hub-panel" role="region" aria-label="Top hub signal">
      <div className="th-header">
        <span className="th-badge">Top Hub</span>
        <span className="th-hub">{signal.label}</span>
        <span className="th-rank">Rank {signal.rank}</span>
      </div>

      <div className="th-body">
        <div className="th-stats">
          <span>{signal.connections} connections</span>
          <span className="th-source">via {signal.source}</span>
        </div>

        {signal.context && (
          <div className="th-context">
            <p className="th-summary">{signal.context.summary}</p>
            {signal.context.actionItems?.length > 0 && (
              <ul className="th-actions">
                {signal.context.actionItems.map((ai, i) => (
                  <li key={i}>{ai}</li>
                ))}
              </ul>
            )}
            {signal.context.docs?.length > 0 && (
              <div className="th-docs">
                {signal.context.docs.map((d, i) => (
                  <a key={i} href={d.url} target="_blank" rel="noopener noreferrer" className="th-doc">
                    {d.title}
                  </a>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="th-footer">
        <small>Updated {new Date(signal.lastUpdated).toLocaleString()}</small>
      </div>
    </div>
  );
}
```

#### D) Minimal styles: `/opt/axentx/Costinel/src/components/TopHubSignalPanel.css`
Keeps panel compact and non-intrusive.
