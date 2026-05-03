# Costinel / frontend

**Final Synthesized Plan — Highest-Value Frontend Increment (<2h)**

**Chosen approach (unified):**  
Add a **non-blocking Top-Hub Signal Panel** that surfaces the most-connected hub (e.g., “MOC”) using **CDN-first, build-time baked data**. Runtime dashboard makes **zero Hugging Face API calls** and zero auth/rate-limit risk. Degrades gracefully if CDN data is missing.

---

### Why this wins
- Pure frontend + build-time asset (no backend changes).
- CDN bypass avoids auth/rate limits and keeps runtime fast.
- Non-blocking and SSR-friendly; safe fallback keeps UX intact.
- Fits existing patterns (`#knowledge-rag #graph #hub`).
- Deliverable in <2 hours.

---

### Concrete implementation (single source of truth)

#### 1) Build-time asset (orchestration)
Create `scripts/build-top-hub.js` and wire into `package.json`.

```json
"scripts": {
  "prebuild": "node scripts/build-top-hub.js",
  "build": "vite build"
}
```

`scripts/build-top-hub.js`
```js
#!/usr/bin/env node
/**
 * Build-time step: fetch top-hub snapshot via HF CDN (no auth/rate-limit)
 * Output: public/data/top-hub.json
 *
 * Usage: node scripts/build-top-hub.js
 * CI: run before `npm run build`
 */
const fs = require('fs');
const path = require('path');
const https = require('https');

const REPO = 'axentx/costinel-knowledge';
const FILE_PATH = 'top-hub/latest.json';
const CDN_URL = `https://huggingface.co/datasets/${REPO}/resolve/main/${FILE_PATH}`;
const OUT_DIR = path.resolve(__dirname, '../public/data');
const OUT_PATH = path.join(OUT_DIR, 'top-hub.json');

function fetchJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, (res) => {
      if (res.statusCode === 302 || res.statusCode === 301) {
        return fetchJson(res.headers.location).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`HTTP ${res.statusCode} fetching ${url}`));
      }
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => (data += chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (err) {
          reject(new Error('Invalid JSON from CDN: ' + err.message));
        }
      });
    });
    req.on('error', reject);
    req.setTimeout(8000, () => {
      req.destroy();
      reject(new Error('Timeout fetching CDN asset'));
    });
  });
}

async function main() {
  try {
    if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });

    const payload = await fetchJson(CDN_URL);

    // Minimal contract expected by frontend
    const normalized = {
      generatedAt: new Date().toISOString(),
      repo: REPO,
      topHub: payload.hub || 'MOC',
      title: payload.title || 'Most-Connected Hub',
      summary: payload.summary || 'Review the central hub before planning tasks.',
      updatedAt: payload.updatedAt || new Date().toISOString(),
      tags: Array.isArray(payload.tags) ? payload.tags : ['#knowledge-rag', '#graph', '#hub'],
      insights: Array.isArray(payload.insights) ? payload.insights.slice(0, 6) : [],
      paths: Array.isArray(payload.paths) ? payload.paths.slice(0, 12) : [],
    };

    fs.writeFileSync(OUT_PATH, JSON.stringify(normalized, null, 2), 'utf8');
    console.log('✅ Built public/data/top-hub.json from CDN');
  } catch (err) {
    // Non-blocking: safe fallback so build never breaks
    if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
    const fallback = {
      generatedAt: new Date().toISOString(),
      repo: REPO,
      topHub: 'MOC',
      title: 'Most-Connected Hub',
      summary: 'Review the central hub before planning tasks.',
      updatedAt: new Date().toISOString(),
      tags: ['#knowledge-rag', '#graph', '#hub'],
      insights: [],
      paths: [],
      _fallback: true,
    };
    fs.writeFileSync(OUT_PATH, JSON.stringify(fallback, null, 2), 'utf8');
    console.warn('⚠️ CDN fetch failed; using fallback top-hub.json', err.message);
  }
}

if (require.main === module) {
  main();
}
```

---

#### 2) Top-Hub Signal Panel component
Create `src/components/TopHubSignalPanel.tsx` (React + TypeScript). Lightweight, non-blocking, SSR-safe.

```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface TopHubData {
  topHub: string;
  title: string;
  summary: string;
  updatedAt: string;
  tags: string[];
  insights: string[];
  paths: string[];
  _fallback?: boolean;
}

const DEFAULT_DATA: TopHubData = {
  topHub: 'MOC',
  title: 'Most-Connected Hub',
  summary: 'Review the central hub before planning tasks.',
  updatedAt: new Date().toISOString(),
  tags: ['#knowledge-rag', '#graph', '#hub'],
  insights: [],
  paths: [],
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/data/top-hub.json', { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load top-hub.json');
        return res.json();
      })
      .then((json) => {
        if (mounted) {
          setData({
            topHub: json.topHub || DEFAULT_DATA.topHub,
            title: json.title || DEFAULT_DATA.title,
            summary: json.summary || DEFAULT_DATA.summary,
            updatedAt: json.updatedAt || DEFAULT_DATA.updatedAt,
            tags: Array.isArray(json.tags) ? json.tags : DEFAULT_DATA.tags,
            insights: Array.isArray(json.insights) ? json.insights : DEFAULT_DATA.insights,
            paths: Array.isArray(json.paths) ? json.paths : DEFAULT_DATA.paths,
          });
        }
      })
      .catch(() => {
        // Graceful: render nothing on failure (non-blocking)
        if (mounted) setData(null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <aside className="top-hub-panel skeleton" aria-hidden="true">
        <div className="skeleton-header" />
        <div className="skeleton-line" />
        <div className="skeleton-line short" />
      </aside>
    );
  }

  if (!data) return null;

  return (
    <aside className="top-hub-panel" aria-label="Top hub signal">
      <header className="panel-header">
        <h3 className="panel-title">{data.title}</h3>
        <span className="panel-badge" title={data.topHub}>
          {data.topHub}
        </span>
      </header>

      <p className="panel-summary">{data.summary}</p>

      {data.insights.length > 0 && (
        <ul className="insights-list" aria-label="Key insights">
          {data.insights.map((item, idx) => (
            <li key={idx} className="insight-item">
              {item}
            </li>
          ))}
        </ul>
      )}

      <footer className="panel-footer">
       
