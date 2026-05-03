# Costinel / backend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Ship a resilient, zero-runtime-HF-API “Top Hub” signal panel into Costinel backend + frontend so Costinel can *sense* and *signal* the most-connected hub (e.g., “MOC”) without hitting HF API during request serving.

**Scope (≤2h)**:
1. Add a build-time fetch script (`scripts/fetch-top-hub-cdn.js`) that:
   - Uses HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) to download a small curated top-hub JSON.
   - Writes output to `public/data/top-hub.json` (static asset).
2. Add a tiny backend endpoint (`GET /api/signals/top-hub`) that serves the static JSON with cache headers.
3. Add a lightweight frontend panel component (`TopHubSignalPanel`) that fetches `/api/signals/top-hub` and renders the hub + related docs.
4. Update build/deploy to run the fetch script before asset compilation (or include JSON in repo as fallback).

**Why this fits patterns**:
- Uses HF CDN bypass (no Authorization header) → avoids 429/limits.
- Pre-lists and embeds file paths (single CDN fetch) → zero API calls at runtime.
- Aligns with “sense + signal — ไม่ Execute” (read-only signal panel).
- Reuses top-hub insight pattern (#knowledge-rag #graph #hub).

---

### 1) Add build-time CDN fetch script

`/opt/axentx/Costinel/scripts/fetch-top-hub-cdn.js`

```js
#!/usr/bin/env node
/**
 * Fetch top-hub signal from HF CDN (no auth) and write static asset.
 * Usage: node scripts/fetch-top-hub-cdn.js
 *
 * HF CDN format:
 * https://huggingface.co/datasets/{repo}/resolve/main/{path}
 */

const https = require('https');
const fs = require('fs');
const path = require('path');

// Config — change repo/path as needed. Keep small (<100KB).
const HF_REPO = 'axentx/costinel-signals';
const HF_PATH = 'top-hub/top-hub-latest.json';
const OUT_DIR = path.join(__dirname, '..', 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub.json');

function fetchCdn(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if (res.statusCode === 302 || res.statusCode === 301) {
          return fetchCdn(res.headers.location).then(resolve).catch(reject);
        }
        if (res.statusCode !== 200) {
          return reject(new Error(`HTTP ${res.statusCode} fetching ${url}`));
        }
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          try {
            const text = Buffer.concat(chunks).toString();
            resolve(JSON.parse(text));
          } catch (err) {
            reject(new Error('Invalid JSON from CDN: ' + err.message));
          }
        });
      })
      .on('error', reject);
  });
}

async function run() {
  const url = `https://huggingface.co/datasets/${HF_REPO}/resolve/main/${HF_PATH}`;
  console.log(`Fetching top-hub signal from CDN: ${url}`);
  try {
    const payload = await fetchCdn(url);
    if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(OUT_FILE, JSON.stringify(payload, null, 2), 'utf8');
    console.log(`Wrote ${OUT_FILE} (${fs.statSync(OUT_FILE).size} bytes)`);
  } catch (err) {
    console.warn('CDN fetch failed, preserving existing static fallback (if any):', err.message);
    // If fetch fails, keep existing file or create minimal fallback to avoid runtime crash.
    if (!fs.existsSync(OUT_FILE)) {
      const fallback = { hub: null, docs: [], generatedAt: null, note: 'fallback' };
      fs.writeFileSync(OUT_FILE, JSON.stringify(fallback, null, 2), 'utf8');
      console.log('Created fallback top-hub.json');
    }
  }
}

if (require.main === module) {
  run().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

Make executable:

```bash
chmod +x /opt/axentx/Costinel/scripts/fetch-top-hub-cdn.js
```

---

### 2) Add backend endpoint (Node/Express assumed)

`/opt/axentx/Costinel/src/routes/signals.js` (create or append)

```js
const express = require('express');
const fs = require('fs');
const path = require('path');
const router = express.Router();

const TOP_HUB_PATH = path.join(__dirname, '..', '..', 'public', 'data', 'top-hub.json');

function readTopHub() {
  try {
    if (fs.existsSync(TOP_HUB_PATH)) {
      const raw = fs.readFileSync(TOP_HUB_PATH, 'utf8');
      return JSON.parse(raw);
    }
  } catch (err) {
    // fail gracefully
    console.warn('Failed to read top-hub.json:', err.message);
  }
  return { hub: null, docs: [], generatedAt: null, note: 'unavailable' };
}

/**
 * GET /api/signals/top-hub
 * Returns top hub signal (served from static CDN-baked asset).
 */
router.get('/top-hub', (req, res) => {
  const payload = readTopHub();
  // short cache: 5m; clients can revalidate.
  res.set({
    'Cache-Control': 'public, max-age=300, s-maxage=300, stale-while-revalidate=60',
    'Content-Type': 'application/json'
  });
  res.json(payload);
});

module.exports = router;
```

Register route in main app (e.g., `app.js` or `server.js`):

```js
const signalsRouter = require('./src/routes/signals');
app.use('/api/signals', signalsRouter);
```

---

### 3) Add frontend panel (React example)

`/opt/axentx/Costinel/src/components/TopHubSignalPanel.jsx`

```jsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

export default function TopHubSignalPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let mounted = true;
    fetch('/api/signals/top-hub', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('Network response was not ok');
        return r.json();
      })
      .then((json) => {
        if (mounted) {
          setData(json);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (mounted) {
          setError(err.message);
          setLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) return <div className="top-hub-panel loading">Loading top hub signal…</div>;
  if (error) return <div className="top-hub-panel error">Signal unavailable</div>;
  if (!data || !data.hub) return <div className="top-hub-panel empty">No top hub signal</div>;

  return (
    <div className="top-hub-panel">
      <div className="top-hub-header">
        <h3>Top Hub</h3>
        <span className="hub-name">{data.hub}</span>
        {data.generatedAt && (
          <time className="generated-at" dateTime={data.generatedAt}>
            Updated {new Date(data.generatedAt).toLocaleString()}
          </time>
        )}
      </div>

