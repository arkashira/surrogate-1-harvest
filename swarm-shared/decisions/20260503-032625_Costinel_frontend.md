# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected hub (default `MOC`) from a local knowledge-rag graph snapshot.
- Uses **HF CDN bypass** (`resolve/main/...`) for zero-API data fetches during runtime.
- Renders as a dismissible, low-priority card in the cost dashboard so it never blocks core workflows.
- Graceful fallback to `MOC` with minimal UI if CDN or graph file is unavailable.

### Architecture (fits existing patterns)
- **Data source**: `knowledge-rag` export → `batches/mirror-merged/{date}/top-hubs.json` (projected `{hub,score,edges}`).
- **Delivery**: Public CDN path — `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/batches/mirror-merged/{YYYY-MM-DD}/top-hubs.json`
- **Runtime**: Dashboard fetches once per session via CDN; caches in `localStorage` (1h TTL).
- **No API calls during render** — avoids HF rate limits.
- **No backend changes** — pure frontend addition.

### File changes (est. 1–1.5h)
1. **Create** `src/lib/topHubSignal.ts` — CDN fetcher + cache + fallback.
2. **Create** `src/components/TopHubSignalPanel.tsx` — dismissible card UI.
3. **Update** `src/pages/Dashboard.tsx` (or main layout) — mount panel below primary cost summary.
4. **Add** i18n keys (if i18n exists) or inline English (safe for now).

---

## Code Snippets

### 1) CDN fetcher + cache (`src/lib/topHubSignal.ts`)
```ts
// src/lib/topHubSignal.ts
const CDN_ROOT = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main';
const FALLBACK = { hub: 'MOC', score: 1, edges: [] as string[] };

export interface TopHub {
  hub: string;
  score: number;
  edges: string[];
}

function getDateFolder(): string {
  const d = new Date();
  // Use yesterday if today not published yet; simple.
  const yesterday = new Date(d.getTime() - 24 * 60 * 60 * 1000);
  return yesterday.toISOString().slice(0, 10); // YYYY-MM-DD
}

export function buildTopHubsUrl(date?: string): string {
  const folder = date || getDateFolder();
  return `${CDN_ROOT}/batches/mirror-merged/${folder}/top-hubs.json`;
}

const CACHE_KEY = 'costinel:top-hub:v1';
const TTL_MS = 60 * 60 * 1000; // 1h

function getCached(): TopHub | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { ts, value } = JSON.parse(raw);
    if (Date.now() - ts > TTL_MS) return null;
    return value as TopHub;
  } catch {
    return null;
  }
}

function setCached(value: TopHub) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), value }));
  } catch {
    // ignore storage limits
  }
}

export async function fetchTopHub(date?: string): Promise<TopHub> {
  const cached = getCached();
  if (cached) return cached;

  const url = buildTopHubsUrl(date);
  try {
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const json = await res.json();

    // Accept either single object or array; pick highest score.
    let hub: TopHub;
    if (Array.isArray(json)) {
      hub = json.sort((a, b) => (b.score || 0) - (a.score || 0))[0];
    } else if (json && typeof json === 'object') {
      hub = json;
    } else {
      throw new Error('Invalid top-hubs payload');
    }

    // Minimal validation
    if (!hub.hub || typeof hub.score !== 'number') throw new Error('Invalid hub shape');
    setCached(hub);
    return hub;
  } catch (err) {
    console.warn('[TopHubSignal] CDN fetch failed, using fallback:', err);
    setCached(FALLBACK);
    return FALLBACK;
  }
}
```

### 2) Dismissible panel component (`src/components/TopHubSignalPanel.tsx`)
```tsx
// src/components/TopHubSignalPanel.tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHub, TopHub } from '../lib/topHubSignal';
import './TopHubSignalPanel.css';

const DISMISS_KEY = 'costinel:top-hub-panel:dismissed';

function isDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === '1';
  } catch {
    return false;
  }
}

function setDismissed() {
  try {
    localStorage.setItem(DISMISS_KEY, '1');
  } catch {}
}

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);
  const [dismissed, setDismissedState] = useState(isDismissed());

  useEffect(() => {
    if (dismissed) {
      setLoading(false);
      return;
    }
    let mounted = true;
    fetchTopHub()
      .then((h) => {
        if (mounted) setHub(h);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [dismissed]);

  if (dismissed) return null;
  if (loading && !hub) return null; // non-blocking: render nothing while loading

  const h = hub || FALLBACK_FOR_RENDER;
  const edgeCount = h.edges?.length ?? 0;

  return (
    <div className="top-hub-panel" role="complementary" aria-label="Top knowledge hub signal">
      <div className="top-hub-panel__content">
        <div className="top-hub-panel__header">
          <span className="top-hub-panel__badge">Top Hub</span>
          <strong className="top-hub-panel__hub">{h.hub}</strong>
          <button
            className="top-hub-panel__close"
            onClick={() => {
              setDismissed();
              setDismissedState(true);
            }}
            aria-label="Dismiss top hub signal"
          >
            ×
          </button>
        </div>
        <div className="top-hub-panel__body">
          <div className="top-hub-panel__score">Relevance: {h.score.toFixed(2)}</div>
          <div className="top-hub-panel__edges">
            {edgeCount > 0 ? `${edgeCount} connected nodes` : 'No edges reported'}
          </div>
          <div className="top-hub-panel__note">
            Sense + Signal — ไม่ Execute. Review before governance actions.
          </div>
        </div>
      </div>
    </div>
  );
}

const FALLBACK_FOR_RENDER = { hub: 'MOC', score: 1, edges: [] };
```

### 3) Minimal CSS (`src/components/TopHubSignalPanel.css`)
```css
/* src/components/TopHubSignalPanel.css */
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  background: #fbfdff;
  margin: 12px 0;
  padding: 12px;
  font-size: 13px;
  color: #24292f;
}

.top-hub-panel__content {
  display: flex;
  flex-direction: column
