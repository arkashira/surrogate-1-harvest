# Costinel / quality

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a resilient, zero-runtime-HF-API “Top Hub Signal” panel to Costinel that surfaces the most-connected hub (e.g., MOC) with contextual insights, using CDN-baked data and robust offline-first fallbacks.

---

### Scope (what ships)
- **Frontend**: `TopHubSignalPanel` (client-side, CDN-first, SSR-safe)
- **Static data**: `public/data/top-hub.json` (committed; served via CDN)
- **Build script**: `scripts/bake-top-hub.js` (run on Mac/CI after rate-limit window)
- **Fallbacks**: baked local JSON → static placeholder → graceful empty state
- **No runtime HF API**, no new backend endpoints, no DB migrations

---

### Data contract — `public/data/top-hub.json`

```json
{
  "hub": {
    "id": "MOC",
    "title": "MOC — Map of Content",
    "description": "Most-connected hub for Costinel governance patterns and decision records.",
    "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/MOC.md",
    "connections": 42,
    "lastUpdated": "2026-05-03T04:13:13.000Z",
    "tags": ["#knowledge-rag", "#graph", "#hub", "#top-hub"]
  },
  "insights": [
    "Prefer CDN-first reads for datasets with mixed schemas to avoid HF API 429.",
    "Reuse Lightning Studio sessions to preserve quota; avoid idle-stop training loss.",
    "Use deterministic repo hashing to spread HF commit writes across sibling repos."
  ]
}
```

---

### Build script — `scripts/bake-top-hub.js`

Run on orchestration node (Mac/CI) after rate-limit window clears. Uses a single `list_repo_tree` call, then CDN fetch for the selected hub file. Produces safe fallback JSON on failure.

```js
#!/usr/bin/env node
/**
 * Bake top-hub.json for Costinel (CDN-first, zero runtime HF API).
 * Usage: HF_TOKEN=xxx node scripts/bake-top-hub.js
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import fetch from 'node-fetch';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repo = 'axentx/knowledge-rag';
const hubFolder = 'hubs';
const outFile = path.resolve(__dirname, '../public/data/top-hub.json');

async function hfApi(subpath) {
  const res = await fetch(`https://huggingface.co/api/${subpath}`, {
    headers: { Authorization: `Bearer ${process.env.HF_TOKEN || ''}` }
  });
  if (res.status === 429) {
    const retryAfter = Number(res.headers.get('retry-after') || 360);
    throw new Error(`HF API 429 — retry after ${retryAfter}s`);
  }
  if (!res.ok) throw new Error(`HF API ${res.status} ${await res.text()}`);
  return res.json();
}

async function cdnFetch(filePath) {
  const url = `https://huggingface.co/datasets/${repo}/resolve/main/${filePath}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`CDN fetch ${res.status} for ${filePath}`);
  return res.text();
}

function pickTopHub(tree) {
  const hubs = tree
    .filter((f) => f.type === 'file' && f.path.startsWith(`${hubFolder}/`) && f.path.endsWith('.md'))
    .map((f) => ({
      path: f.path,
      name: path.basename(f.path, '.md'),
      size: f.size
    }))
    .sort((a, b) => b.size - a.size);
  return hubs[0] || null;
}

function safePayload(fallback) {
  return {
    hub: {
      id: 'MOC',
      title: 'MOC — Map of Content',
      description: fallback ? 'Top hub (fallback).' : 'Most-connected hub for governance patterns.',
      url: `https://huggingface.co/datasets/${repo}/resolve/main/${hubFolder}/MOC.md`,
      connections: fallback ? 0 : 42,
      lastUpdated: new Date().toISOString(),
      tags: ['#knowledge-rag', '#graph', '#hub', '#top-hub']
    },
    insights: fallback ? [] : [
      'Prefer CDN-first reads for datasets with mixed schemas to avoid HF API 429.',
      'Reuse Lightning Studio sessions to preserve quota; avoid idle-stop training loss.',
      'Use deterministic repo hashing to spread HF commit writes across sibling repos.'
    ]
  };
}

async function main() {
  try {
    const tree = await hfApi(`datasets/${repo}/tree?path=${encodeURIComponent(hubFolder)}&recursive=false`);
    const top = pickTopHub(tree);
    if (!top) throw new Error('No hub files found');

    const content = await cdnFetch(top.path);
    const firstLine = content.split('\n').find((l) => l.trim()) || '';
    const connections = (content.match(/#hub|#knowledge-rag|#graph/g) || []).length;

    const payload = {
      hub: {
        id: top.name.toUpperCase(),
        title: `${top.name.toUpperCase()} — Map of Content`,
        description: firstLine.replace(/^#+\s*/, '').trim() || `Most-connected hub for governance patterns.`,
        url: `https://huggingface.co/datasets/${repo}/resolve/main/${top.path}`,
        connections,
        lastUpdated: new Date().toISOString(),
        tags: ['#knowledge-rag', '#graph', '#hub', '#top-hub']
      },
      insights: [
        'Prefer CDN-first reads for datasets with mixed schemas to avoid HF API 429.',
        'Reuse Lightning Studio sessions to preserve quota; avoid idle-stop training loss.',
        'Use deterministic repo hashing to spread HF commit writes across sibling repos.'
      ]
    };

    fs.mkdirSync(path.dirname(outFile), { recursive: true });
    fs.writeFileSync(outFile, JSON.stringify(payload, null, 2));
    console.log('Baked top-hub.json:', outFile);
  } catch (err) {
    console.error('Bake failed:', err.message);
    fs.mkdirSync(path.dirname(outFile), { recursive: true });
    fs.writeFileSync(outFile, JSON.stringify(safePayload(true), null, 2));
    // Exit success so CI continues; panel will use fallback.
  }
}

main();
```

---

### Frontend panel — `TopHubSignalPanel.jsx`

CDN-first, SSR-safe, robust fallbacks, small UX polish (card, icon, timestamp, refresh hint).

```jsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

function timeAgo(iso) {
  try {
    const d = new Date(iso);
    const mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(d);
  } catch {
    return '';
  }
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        // CDN-first, no auth
        const res = await fetch('/data/top-hub.json', { cache: 'no-store' });
        if (!res.ok) throw new Error('Network response not ok');
        const json = await res.json();
        if (!cancelled) setData(json);
      } catch {
        // graceful empty state handled below
        if (!cancelled) setData
