# Costinel / discovery

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected hub (default `MOC`) from a lightweight hub-graph index
- Shows 3 contextual insights from knowledge-rag (cached via CDN)
- Zero API calls during dashboard render (CDN-only fetch)
- Graceful fallback when index unavailable

### Architecture (fits existing patterns)
- **Mac orchestrator**: one-time `list_repo_tree` → `hub-index.json` (committed to repo or CI artifact)
- **CDN fetch**: dashboard loads `https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/hub-index.json` (no auth, no rate limit)
- **Panel**: React component in dashboard, SSR-safe, 3-column insight cards
- **CI**: regenerate hub-index nightly via GitHub Action (or on-demand script)

---

### File changes (concrete)

#### 1) Hub index generator (run on Mac / CI)
`scripts/generate-hub-index.js`
```js
#!/usr/bin/env node
/**
 * Generate lightweight hub-graph index for CDN-first lookup.
 * Usage: node scripts/generate-hub-index.js > knowledge/hub-index.json
 *
 * Output:
 * {
 *   "generatedAt": "2026-05-03T03:30:00.000Z",
 *   "topHub": "MOC",
 *   "hubs": {
 *     "MOC": {
 *       "connections": 42,
 *       "insights": [
 *         "MOC drives 38% of cross-account tagging compliance drift",
 *         "Reserved Instance coverage gaps cluster around MOC-linked accounts",
 *         "Anomaly spike correlation: MOC + ap-southeast-1 + EBS snapshot growth"
 *       ],
 *       "tags": ["knowledge-rag","graph","hub"]
 *     }
 *   }
 * }
 */

const fs = require('fs');
const path = require('path');

// Lightweight heuristic: pick most referenced tag/hub from existing markdown/knowledge files
function buildFromKnowledgeTree(root) {
  const hubs = {};
  const files = fs.readdirSync(root, { recursive: true }).filter(f =>
    f.endsWith('.md') || f.endsWith('.txt')
  );

  for (const f of files) {
    const content = fs.readFileSync(path.join(root, f), 'utf8');
    // naive hub extraction: lines with "hub" or uppercase acronym patterns
    const matches = content.match(/[A-Z]{2,4}(?=\s+hub|\s+#hub|\s+\|)/g) || [];
    for (const m of matches) {
      hubs[m] = hubs[m] || { connections: 0, insights: [], tags: [] };
      hubs[m].connections += 1;
    }
  }

  // default fallback
  if (Object.keys(hubs).length === 0) {
    hubs.MOC = {
      connections: 1,
      insights: [
        "MOC identified as primary governance hub (default).",
        "Review top-hub doc insight (2026-04-27) before planning tasks.",
        "Enable knowledge-rag pipeline for contextual recommendations."
      ],
      tags: ["knowledge-rag","graph","hub"]
    };
  }

  // pick top by connections
  const topKey = Object.entries(hubs).sort((a,b) => b[1].connections - a[1].connections)[0][0];

  return {
    generatedAt: new Date().toISOString(),
    topHub: topKey,
    hubs
  };
}

const index = buildFromKnowledgeTree(path.resolve(__dirname, '../knowledge'));
process.stdout.write(JSON.stringify(index, null, 2));
```

Make executable and add to package.json script:
```bash
chmod +x scripts/generate-hub-index.js
```

`package.json` (add)
```json
"scripts": {
  "gen:hub-index": "node scripts/generate-hub-index.js > knowledge/hub-index.json"
}
```

---

#### 2) Hub index loader (CDN-first, zero API during render)
`src/lib/hubIndex.js`
```js
/**
 * CDN-first hub index loader.
 * Uses public CDN URL to bypass HF API rate limits.
 * Falls back to local build-time copy if CDN fails.
 */
const CDN_URL = 'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/knowledge/hub-index.json';
const LOCAL_FALLBACK = typeof window === 'undefined'
  ? require('../knowledge/hub-index.json')
  : null;

export async function fetchHubIndex(options = {}) {
  const { timeout = 4000 } = options;

  // SSR: use local fallback
  if (typeof window === 'undefined' && LOCAL_FALLBACK) {
    return LOCAL_FALLBACK;
  }

  // Browser: CDN fetch with timeout
  try {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);

    const res = await fetch(CDN_URL, {
      signal: controller.signal,
      cache: 'force-cache' // allow stale-while-revalidate
    });
    clearTimeout(id);

    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    return await res.json();
  } catch (err) {
    // graceful fallback to inlined/generated index if available
    if (window.__HUB_INDEX_FALLBACK__) {
      return window.__HUB_INDEX_FALLBACK__;
    }
    console.warn('Hub index unavailable, using minimal default', err);
    return {
      generatedAt: new Date().toISOString(),
      topHub: 'MOC',
      hubs: {
        MOC: {
          connections: 0,
          insights: [
            'Costinel: Sense + Signal — no direct execution.',
            'Enable knowledge-rag for contextual hub insights.',
            'Review top-hub doc insight (2026-04-27) before planning.'
          ],
          tags: ['knowledge-rag','graph','hub']
        }
      }
    };
  }
}
```

---

#### 3) Top-Hub Signal Panel component
`src/components/TopHubSignalPanel.jsx`
```jsx
import React, { useEffect, useState } from 'react';
import { fetchHubIndex } from '../lib/hubIndex';

export default function TopHubSignalPanel({ className = '' }) {
  const [index, setIndex] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetchHubIndex().then(data => {
      if (mounted) {
        setIndex(data);
        setLoading(false);
      }
    }).catch(() => setLoading(false));

    return () => { mounted = false; };
  }, []);

  if (loading) {
    return (
      <div className={`top-hub-panel skeleton ${className}`} aria-busy="true">
        <div className="skeleton-header" />
        <div className="skeleton-cards" />
      </div>
    );
  }

  const topHub = index?.topHub || 'MOC';
  const hub = index?.hubs?.[topHub] || index?.hubs?.MOC;
  const insights = hub?.insights || [];

  return (
    <section className={`top-hub-panel ${className}`} aria-label="Top hub signals">
      <header className="panel-header">
        <h3>Top Hub: <strong>{topHub}</strong></h3>
        <span className="badge">{hub?.connections || 0} connections</span>
        <time className="muted" dateTime={index?.generatedAt}>
          {index?.generatedAt ? new Date(index.generatedAt).toLocaleDateString() : ''}
        </time>
      </header>

      <div className="insights-grid" role="list">
        {insights.slice(0, 3).map((text, i) => (
          <article key={i} className="insight-card" role="listitem">
            <p>{text}</p>
            <div className="card-tags">
              {(hub?.tags || []).map
