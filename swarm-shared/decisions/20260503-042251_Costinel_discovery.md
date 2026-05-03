# Costinel / discovery

## Final Synthesis — CDN-First Top-Hub Signal Panel (<2h)

**Chosen approach**: zero-runtime-HF-API, baked CDN artifact, resilient UI, deployable in ≤2 hours with no backend changes.  
Contradictions resolved in favor of **correctness** (CDN bypass, no auth/rate-limit) and **concrete actionability** (single repo path, clear caching, non-blocking fallback).

---

### 1) Data contract (10m)
Use a single, minimal JSON served from **`public/data/top-hub.json`** (not `public/signals/` — pick one canonical path to avoid routing ambiguity).  
Schema (final):

```json
{
  "hub": "MOC",
  "connections": 142,
  "summary": "Most-connected hub for cost-governance signals. Central to anomaly detection and recommendation routing.",
  "updated": "2026-05-03T04:20:00Z",
  "links": [
    { "label": "Overview", "href": "/hubs/moc" },
    { "label": "Insights", "href": "/hubs/moc/insights" }
  ],
  "tags": ["knowledge-rag", "graph", "hub"]
}
```

- `updated` (ISO string) preferred over `lastUpdated` for consistency with Date handling.
- Keep `links` for immediate actionability; `tags` optional for future filtering.

---

### 2) Bake artifact (15m)
Place generator at **`scripts/bake-top-hub.js`**. Behavior:

- Prefer **local graph export** (fast, offline) and fall back to HF CDN `resolve/main/` **read-only** fetch (no HF API auth/rate risk).
- Output exactly the schema above to `public/data/top-hub.json`.
- Commit-friendly: deterministic, idempotent, no secrets.

```js
#!/usr/bin/env node
/**
 * Bake top-hub.json from local graph export or HF CDN (read-only).
 * Usage: node scripts/bake-top-hub.js
 * Writes: public/data/top-hub.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const GRAPH_PATH = path.resolve(__dirname, '../graph-export.json');
const OUT_DIR = path.resolve(__dirname, '../public/data');
const OUT_PATH = path.join(OUT_DIR, 'top-hub.json');

function computeTopHubLocal(graph) {
  const degree = {};
  (graph.edges || []).forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1;
    degree[e.target] = (degree[e.target] || 0) + 1;
  });
  const entries = Object.entries(degree);
  const hub = entries.length ? entries.sort((a, b) => b[1] - a[1])[0] : ['MOC', 0];
  return { hub: hub[0], connections: hub[1] };
}

function safeFetchJSON(url) {
  return new Promise((resolve) => {
    const req = https.get(url, { timeout: 8000 }, (res) => {
      if (res.statusCode !== 200) return resolve(null);
      let body = '';
      res.on('data', (chunk) => (body += chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch {
          resolve(null);
        }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
  });
}

async function run() {
  let top = { hub: 'MOC', connections: 0 };

  // 1) Try local graph
  if (fs.existsSync(GRAPH_PATH)) {
    try {
      const graph = JSON.parse(fs.readFileSync(GRAPH_PATH, 'utf8'));
      top = computeTopHubLocal(graph);
    } catch {
      // ignore
    }
  }

  // 2) Optional: try HF CDN read-only path (no HF API)
  // Replace with your CDN URL pattern if needed
  if (top.connections === 0) {
    const cdnUrl = 'https://huggingface.co/datasets/your-owner/your-repo/resolve/main/graph-export.json';
    const remote = await safeFetchJSON(cdnUrl);
    if (remote) top = computeTopHubLocal(remote);
  }

  const payload = {
    hub: top.hub,
    connections: top.connections,
    summary: 'Most-connected hub for cost-governance signals. Central to anomaly detection and recommendation routing.',
    updated: new Date().toISOString(),
    links: [
      { label: 'Overview', href: `/hubs/${top.hub.toLowerCase()}` },
      { label: 'Insights', href: `/hubs/${top.hub.toLowerCase()}/insights` }
    ],
    tags: ['knowledge-rag', 'graph', 'hub']
  };

  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_PATH, JSON.stringify(payload, null, 2));
  console.log('Baked:', OUT_PATH);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

- Add to `package.json`: `"bake:top-hub": "node scripts/bake-top-hub.js"`.

---

### 3) UI component (45m)
Create **`components/TopHubSignal.tsx`** (non-blocking, CDN-first):

- Fetch `/data/top-hub.json` with abort + timeout.
- Skeleton while loading; **non-blocking empty state** on error (do not crash UI).
- Respect theme and mobile responsiveness.

```tsx
'use client';

import { useEffect, useState } from 'react';

interface HubLink {
  label: string;
  href: string;
}

interface TopHubData {
  hub: string;
  connections: number;
  summary: string;
  updated: string;
  links: HubLink[];
  tags?: string[];
}

export default function TopHubSignal() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);

    fetch('/data/top-hub.json', { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error('Top hub unavailable');
        return res.json();
      })
      .then((json) => {
        setData(json);
        setError(false);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false))
      .finally(() => clearTimeout(timeout));

    return () => controller.abort();
  }, []);

  if (loading) {
    return (
      <div className="animate-pulse rounded-lg bg-gray-200 dark:bg-gray-700 p-3">
        <div className="h-4 w-24 mb-2 rounded bg-gray-300 dark:bg-gray-600" />
        <div className="h-3 w-32 rounded bg-gray-300 dark:bg-gray-600" />
      </div>
    );
  }

  if (error || !data) {
    // Non-blocking: do not occupy layout when unavailable
    return null;
  }

  return (
    <div className="rounded-lg border bg-card p-3 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-muted-foreground">Top hub</p>
          <p className="text-lg font-semibold">{data.hub}</p>
          <p className="text-xs text-muted-foreground">{data.connections} connections</p>
        </div>
      </div>
      <p className="mt-2 text-sm text-muted-foreground">{data.summary}</p>
      <div className="mt-2 flex gap-3">
        {data.links.map((link) => (
          <a
           
