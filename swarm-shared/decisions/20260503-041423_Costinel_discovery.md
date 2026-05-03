# Costinel / discovery

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a zero-runtime-HF-API “Top Hub Signal” panel to Costinel that surfaces the most-connected hub (e.g., MOC) with contextual insights, using CDN-baked data and robust fallbacks.

### Scope (incremental, <2h)
- Add a small server-side build step that fetches a pre-baked `top-hub.json` from a CDN path (no auth, no HF API at runtime).
- Add a React panel component (`TopHubSignal`) that renders hub title, short insight, and related doc links.
- Add graceful fallbacks: local static JSON, empty state, and no breaking changes.
- Wire into existing dashboard layout (likely sidebar or top bar area).

### File changes
1. `scripts/fetch-top-hub.js` — build-time CDN fetcher + local fallback.
2. `public/data/top-hub.json` — static fallback committed to repo.
3. `src/components/TopHubSignal.jsx` — presentational panel.
4. `src/pages/Dashboard.jsx` (or similar) — mount the panel.
5. `package.json` — optional build script hook.

---

## 1) Static fallback data (public/data/top-hub.json)

```json
{
  "hub": "MOC",
  "title": "Map of Content — Cost Governance",
  "insight": "Central index of cost-ownership maps, RI coverage patterns, and anomaly playbooks. Use this hub to triage signals before creating proposals.",
  "related": [
    { "label": "RI Coverage Guide", "url": "https://axentx.github.io/Costinel/guides/ri-coverage" },
    { "label": "Anomaly Playbook", "url": "https://axentx.github.io/Costinel/playbooks/anomalies" },
    { "label": "Change Management SOP", "url": "https://axentx.github.io/Costinel/sop/change-mgmt" }
  ],
  "source": "fallback",
  "fetchedAt": "2026-05-03T00:00:00.000Z"
}
```

---

## 2) Build-time CDN fetcher (scripts/fetch-top-hub.js)

```bash
#!/usr/bin/env bash
# scripts/fetch-top-hub.js
# Node script to fetch baked top-hub.json from CDN and write to public/data/top-hub.json
# Usage: node scripts/fetch-top-hub.js
# Falls back to existing file if CDN unavailable (non-zero exit only on fs errors).
```

```js
#!/usr/bin/env node
// scripts/fetch-top-hub.js
const fs = require('fs');
const path = require('path');
const https = require('https');

// Configurable via env
const CDN_URL = process.env.TOP_HUB_CDN_URL || 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json';
const OUT_PATH = path.resolve(__dirname, '../public/data/top-hub.json');

function fetchJson(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if (res.statusCode === 302 || res.statusCode === 301) {
          return fetchJson(res.headers.location).then(resolve, reject);
        }
        if (res.statusCode !== 200) {
          return reject(new Error(`HTTP ${res.statusCode}`));
        }
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          try {
            resolve(JSON.parse(body));
          } catch (err) {
            reject(err);
          }
        });
      })
      .on('error', reject)
      .setTimeout(8000, () => reject(new Error('timeout')));
  });
}

async function run() {
  try {
    const data = await fetchJson(CDN_URL);
    // Minimal validation
    if (!data || typeof data !== 'object' || !data.hub) {
      throw new Error('Invalid top-hub payload');
    }
    fs.mkdirSync(path.dirname(OUT_PATH), { recursive: true });
    fs.writeFileSync(OUT_PATH, JSON.stringify(data, null, 2), 'utf8');
    console.log('Updated top-hub.json from CDN');
  } catch (err) {
    console.warn('Could not fetch top-hub from CDN, keeping existing file:', err.message);
    // Non-zero exit only if we cannot read existing fallback (so build fails visibly)
    if (!fs.existsSync(OUT_PATH)) {
      console.error('No fallback top-hub.json found. Create public/data/top-hub.json.');
      process.exit(1);
    }
  }
}

if (require.main === module) {
  run();
}
```

Make executable and ensure Node usage (safe for CI/local):

```bash
chmod +x scripts/fetch-top-hub.js
```

---

## 3) React panel component (src/components/TopHubSignal.jsx)

```jsx
import React, { useEffect, useState } from 'react';
import './TopHubSignal.css';

export default function TopHubSignal() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Load baked file from public directory (no runtime API calls)
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('no baked file');
        return r.json();
      })
      .then((data) => {
        setHub(data);
        setLoading(false);
      })
      .catch(() => {
        // If baked file missing, try to import fallback JSON (bundled)
        import('../data/top-hub.json')
          .then((mod) => {
            setHub(mod.default || mod);
            setLoading(false);
          })
          .catch(() => setLoading(false));
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-signal loading">
        <div className="shimmer"></div>
      </div>
    );
  }

  if (!hub) {
    return null; // silent fail — do not break layout
  }

  return (
    <div className="top-hub-signal" title={`Source: ${hub.source || 'baked'}`}>
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <strong className="top-hub-name">{hub.hub}</strong>
      </div>
      <p className="top-hub-title">{hub.title}</p>
      <p className="top-hub-insight">{hub.insight}</p>
      {hub.related && hub.related.length > 0 && (
        <ul className="top-hub-related">
          {hub.related.map((item, idx) => (
            <li key={idx}>
              <a href={item.url} target="_blank" rel="noopener noreferrer">
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      )}
      <small className="top-hub-meta">
        Updated {hub.fetchedAt ? new Date(hub.fetchedAt).toLocaleDateString() : '—'}
      </small>
    </div>
  );
}
```

Basic styles (src/components/TopHubSignal.css):

```css
.top-hub-signal {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fbfdff;
  max-width: 320px;
}
.top-hub-signal .top-hub-header {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 6px;
}
.top-hub-signal .top-hub-badge {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: #0b63d6;
  font-weight: 600;
}

