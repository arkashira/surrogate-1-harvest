# Costinel / frontend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked at build time (or via a lightweight Mac-side script) and served via CDN (`/resolve/main/...`) so Lightning training and runtime UI never hit HF API rate limits.

---

### Architecture (CDN-first)

1. **Mac orchestrator** (run occasionally):
   - `list_repo_tree(path='knowledge-rag/hubs', recursive=False)` → JSON of date folders
   - Pick latest folder → read `top-hub.json` (or generate from graph)
   - Upload `top-hub.json` to `datasets/AXENTX/Costinel-signals/resolve/main/top-hub.json` (or repo root `assets/`)
   - Commit/push (respect 128/hr cap; use deterministic sibling repo if needed)

2. **Frontend** (Costinel):
   - Fetch `https://huggingface.co/datasets/AXENTX/Costinel-signals/resolve/main/top-hub.json` (or `/Costinel/assets/top-hub.json`) **with no Authorization header** → CDN bypass, no rate-limit.
   - Render a small, dismissible panel in the dashboard sidebar/header.
   - Cache in `localStorage` (TTL 6h) to avoid repeat fetches.
   - Fallback to local stub if CDN fails.

3. **Build-time option** (alternative):
   - Embed `top-hub.json` into the bundle during CI (fetch once, bake into `public/data/`).
   - Zero runtime network cost.

---

### Implementation Steps (frontend only — <2h)

1. Create `public/data/top-hub.json` stub (for immediate local dev).
2. Add `TopHubSignalPanel` component.
3. Add CDN fetcher with localStorage cache + TTL.
4. Mount panel in main dashboard layout.
5. (Optional) Add a small Mac script to update the CDN file (run manually or via cron after HF window clears).

---

### Code Snippets

#### 1) Stub data (public/data/top-hub.json)
```json
{
  "hub": "MOC",
  "label": "Most-Connected Hub",
  "score": 94.7,
  "summary": "Highest betweenness centrality in knowledge graph — prioritize MOC-linked cost anomalies for contextual review.",
  "updatedAt": "2026-05-03T04:00:00.000Z",
  "link": "https://huggingface.co/datasets/AXENTX/Costinel-signals/blob/main/knowledge-rag/hubs/2026-05-03/top-hub.json"
}
```

#### 2) TopHubSignalPanel component (React/TypeScript)
```tsx
// src/components/TopHubSignalPanel.tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

const CDN_URL = 'https://huggingface.co/datasets/AXENTX/Costinel-signals/resolve/main/top-hub.json';
const LOCAL_FALLBACK = '/data/top-hub.json';
const CACHE_KEY = 'costinel:top-hub';
const CACHE_TTL_MS = 6 * 60 * 60 * 1000; // 6h

interface TopHubData {
  hub: string;
  label?: string;
  score?: number;
  summary?: string;
  updatedAt?: string;
  link?: string;
}

interface Cached<T> {
  ts: number;
  data: T;
}

function isCachedValid(cache: Cached<TopHubData> | null): boolean {
  if (!cache) return false;
  return Date.now() - cache.ts < CACHE_TTL_MS;
}

export const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    async function fetchTopHub() {
      try {
        // Try cache first
        const raw = localStorage.getItem(CACHE_KEY);
        let cached: Cached<TopHubData> | null = null;
        if (raw) {
          try {
            cached = JSON.parse(raw);
          } catch {
            cached = null;
          }
        }

        if (isCachedValid(cached)) {
          setData(cached.data);
          setLoading(false);
          return;
        }

        // CDN fetch (no Authorization header)
        const res = await fetch(CDN_URL, { cache: 'no-store' });
        let payload: TopHubData;
        if (res.ok) {
          payload = await res.json();
        } else {
          // fallback to local stub
          const localRes = await fetch(LOCAL_FALLBACK, { cache: 'no-store' });
          payload = await localRes.json();
        }

        const cachedItem: Cached<TopHubData> = { ts: Date.now(), data: payload };
        localStorage.setItem(CACHE_KEY, JSON.stringify(cachedItem));
        setData(payload);
      } catch (err) {
        console.warn('TopHubSignalPanel: failed to load', err);
        setData(null);
      } finally {
        setLoading(false);
      }
    }

    fetchTopHub();
  }, []);

  if (!visible || loading || !data) return null;

  return (
    <div className="top-hub-panel">
      <div className="top-hub-panel__header">
        <span className="top-hub-panel__label">{data.label || 'Top Hub'}</span>
        <button
          className="top-hub-panel__close"
          onClick={() => setVisible(false)}
          aria-label="Dismiss"
        >
          ×
        </button>
      </div>
      <div className="top-hub-panel__body">
        <div className="top-hub-panel__hub">{data.hub}</div>
        {typeof data.score === 'number' && (
          <div className="top-hub-panel__score">{data.score.toFixed(1)}</div>
        )}
        {data.summary && <div className="top-hub-panel__summary">{data.summary}</div>}
        {data.link && (
          <a className="top-hub-panel__link" href={data.link} target="_blank" rel="noopener noreferrer">
            View details
          </a>
        )}
      </div>
    </div>
  );
};
```

#### 3) Basic styles (src/components/TopHubSignalPanel.css)
```css
.top-hub-panel {
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-radius: 8px;
  padding: 12px 14px;
  color: #e2e8f0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
  max-width: 320px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.35);
}

.top-hub-panel__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #94a3b8;
  margin-bottom: 6px;
}

.top-hub-panel__close {
  background: none;
  border: none;
  color: #64748b;
  font-size: 18px;
  cursor: pointer;
  line-height: 1;
}

.top-hub-panel__close:hover {
  color: #cbd5e1;
}

.top-hub-panel__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.top-hub-panel
