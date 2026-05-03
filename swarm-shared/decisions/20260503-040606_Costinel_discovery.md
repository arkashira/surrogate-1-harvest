# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build/deploy time (zero HF API calls at runtime).

### Why this is highest-value (<2h)
- Directly applies **top-hub doc insight** and **CDN bypass** patterns.
- Zero runtime API calls → no rate-limit risk, instant load.
- Pure frontend + build-time asset → safe, reversible, <2h.

---

### 1) Build-time data generator (CI / deploy hook)

`scripts/build-top-hub.js`

```js
#!/usr/bin/env node
/**
 * Build-time generator for top-hub signal.
 * CDN-only fetch (no auth) or local fallback.
 *
 * Usage (CI):
 *   node scripts/build-top-hub.js --repo "AXENTX/knowledge-rag" --date "2026-04-27" --out "public/signal/top-hub.json"
 *
 * Local dev:
 *   node scripts/build-top-hub.js --out "public/signal/top-hub.json"
 */

import { writeFileSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const [k, v] = a.split('=');
    return [k.replace(/^--/, ''), v];
  })
);

const REPO = args.repo || process.env.KNOWLEDGE_REPO || 'AXENTX/knowledge-rag';
const DATE = args.date || process.env.KNOWLEDGE_DATE || new Date().toISOString().split('T')[0];
const OUT = args.out || process.env.OUT_PATH || resolve(__dirname, '../public/signal/top-hub.json');

const CDN = (repo, path) =>
  `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;

async function fetchJson(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`CDN fetch ${res.status} ${url}`);
  return res.json();
}

function fallback(date) {
  return {
    hub: 'MOC',
    label: 'MOC — Method of Choice',
    connections: 0,
    summary: 'Primary decision framework for cost governance proposals.',
    date,
    source: 'fallback',
    generated_at: new Date().toISOString()
  };
}

async function build() {
  try {
    const path = `knowledge-graph/top-hub-${DATE}.json`;
    const url = CDN(REPO, path);
    const payload = await fetchJson(url);

    const top = payload.top_hub || payload.hub || payload.most_connected || {};
    const out = {
      hub: top.id || top.label || top.hub || 'MOC',
      label: top.label || top.title || top.id || top.hub || 'MOC',
      connections: Number(top.connections || 0),
      summary: top.summary || top.insight || top.description || 'Review top hub before planning tasks.',
      url: top.url || `https://huggingface.co/datasets/${REPO}/blob/main/hubs/${(top.id || top.label || 'moc').toLowerCase()}.md`,
      date: DATE,
      source: `cdn:${REPO}/${path}`,
      generated_at: new Date().toISOString()
    };

    mkdirSync(dirname(OUT), { recursive: true });
    writeFileSync(OUT, JSON.stringify(out, null, 2), 'utf8');
    console.log('Top-hub signal written to', OUT);
  } catch (err) {
    mkdirSync(dirname(OUT), { recursive: true });
    writeFileSync(OUT, JSON.stringify(fallback(DATE), null, 2), 'utf8');
    console.warn('Top-hub CDN fetch failed, using fallback:', err.message);
  }
}

build();
```

Add to `package.json` scripts:

```json
"scripts": {
  "build:signal": "node scripts/build-top-hub.js"
}
```

CI step (example):

```bash
npm run build:signal
```

---

### 2) Frontend signal panel component

`src/components/TopHubSignalPanel.jsx`

```tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

/**
 * Top-Hub Signal Panel
 * - Loads static JSON baked at build time (public/signal/top-hub.json)
 * - Non-blocking, dismissible, lightweight.
 */
export default function TopHubSignalPanel() {
  const [signal, setSignal] = useState(null);
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    fetch('/signal/top-hub.json', { cache: 'force-cache' })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setSignal)
      .catch(() => setSignal(null));
  }, []);

  if (!signal || !visible) return null;

  return (
    <div className="top-hub-signal" role="region" aria-label="Top hub signal">
      <button
        className="top-hub-signal__close"
        onClick={() => setVisible(false)}
        aria-label="Dismiss top hub signal"
      >
        ×
      </button>
      <div className="top-hub-signal__content">
        <div className="top-hub-signal__badge">Top Hub</div>
        <div className="top-hub-signal__hub">{signal.label}</div>
        <div className="top-hub-signal__meta">
          {signal.connections > 0 && (
            <span>{signal.connections} connections</span>
          )}
          <span className="top-hub-signal__date">{signal.date}</span>
        </div>
        <p className="top-hub-signal__summary">{signal.summary}</p>
        {signal.url && (
          <a
            href={signal.url}
            target="_blank"
            rel="noopener noreferrer"
            className="top-hub-signal__link"
          >
            View hub details
          </a>
        )}
      </div>
    </div>
  );
}
```

`src/components/TopHubSignalPanel.css`

```css
.top-hub-signal {
  position: fixed;
  bottom: 16px;
  right: 16px;
  max-width: 320px;
  background: #fff;
  border: 1px solid #e6e9ef;
  border-radius: 8px;
  box-shadow: 0 6px 18px rgba(16,24,40,0.08);
  z-index: 1200;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

.top-hub-signal__close {
  position: absolute;
  top: 6px;
  right: 8px;
  background: none;
  border: none;
  font-size: 18px;
  cursor: pointer;
  color: #94a3b8;
}

.top-hub-signal__content {
  padding: 12px 16px 14px;
}

.top-hub-signal__badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #0ea5e9;
  margin-bottom: 6px;
}

.top-hub-signal__hub {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 4px;
}

.top-hub-signal__meta {
  display: flex;
  gap: 8px;
  align-items:
