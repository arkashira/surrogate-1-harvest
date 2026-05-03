# Costinel / backend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked into the backend at build time; frontend fetches via CDN URL only.

---

### 1) High-value scope (fits <2h)
- Backend: small build-time script that exports `top-hub.json` (slug, title, rank, edges, lastUpdated) to `public/data/top-hub.json`
- Frontend: new React panel component (`TopHubSignalPanel`) that:
  - fetches `/data/top-hub.json` (CDN path, no auth)
  - renders card with hub name, rank, edge count, and “context” link
  - graceful fallback (skeleton / stale cache) if fetch fails
- No HF API, no auth, no runtime secrets.

---

### 2) Implementation steps

#### A) Add build-time export script
Create `scripts/build-top-hub.js` (run during CI/build).

```js
// scripts/build-top-hub.js
// Usage: node scripts/build-top-hub.js
// Produces: public/data/top-hub.json
const fs = require('fs');
const path = require('path');

async function buildTopHub() {
  // In production, replace this stub with:
  // - single HF list_repo_tree call (Mac orchestrator) saved to file list
  // - or read from local knowledge-rag graph export
  // For now, use deterministic stub so panel ships immediately.
  const topHub = {
    slug: 'MOC',
    title: 'MOC — Method of Choice',
    rank: 1,
    edges: 142,
    contextPath: '/knowledge-rag/hubs/MOC',
    lastUpdated: new Date().toISOString(),
    source: 'knowledge-rag-graph'
  };

  const outDir = path.join(__dirname, '..', 'public', 'data');
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, 'top-hub.json'),
    JSON.stringify(topHub, null, 2),
    'utf8'
  );

  console.log('Built public/data/top-hub.json');
}

if (require.main === module) {
  buildTopHub().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

Add to `package.json` scripts:
```json
"scripts": {
  "build:top-hub": "node scripts/build-top-hub.js",
  "build": "npm run build:top-hub && next build"
}
```

---

#### B) Add API route (optional cache layer)
If you prefer SSR/edge-cached JSON, add `pages/api/top-hub.js` (or `app/api/top-hub/route.js` for App Router). This keeps CDN path simple and allows short TTL caching.

```js
// pages/api/top-hub.js
import fs from 'fs';
import path from 'path';

export default function handler(req, res) {
  try {
    const filePath = path.join(process.cwd(), 'public', 'data', 'top-hub.json');
    const raw = fs.readFileSync(filePath, 'utf8');
    const data = JSON.parse(raw);

    // Short CDN-friendly cache (60s) — adjust as needed
    res.setHeader('Cache-Control', 'public, s-maxage=60, stale-while-revalidate=300');
    res.status(200).json(data);
  } catch (err) {
    res.status(503).json({ error: 'top-hub unavailable' });
  }
}
```

---

#### C) Frontend panel component
Create `components/TopHubSignalPanel.jsx`.

```tsx
// components/TopHubSignalPanel.jsx
import { useEffect, useState } from 'react';
import Link from 'next/link';

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // CDN-first: prefer direct public JSON (zero API/auth). Fallback to /api/top-hub if needed.
    const url = '/data/top-hub.json';
    fetch(url, { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('fetch failed');
        return r.json();
      })
      .then((data) => {
        if (!cancelled) {
          setHub(data);
          setError(false);
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-white p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-2 h-4 w-20 animate-pulse rounded bg-gray-100" />
      </div>
    );
  }

  if (error || !hub) {
    return (
      <div className="rounded-lg border bg-white p-4 shadow-sm">
        <p className="text-sm text-gray-500">Top hub signal unavailable</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Top Hub</p>
          <h3 className="mt-1 text-lg font-semibold text-gray-900">{hub.title}</h3>
          <p className="mt-1 text-sm text-gray-600">
            Rank #{hub.rank} · {hub.edges} connections
          </p>
        </div>
        {hub.contextPath && (
          <Link
            href={hub.contextPath}
            className="text-sm font-medium text-blue-600 hover:underline"
          >
            View
          </Link>
        )}
      </div>
      <p className="mt-3 text-xs text-gray-400">Updated {new Date(hub.lastUpdated).toLocaleString()}</p>
    </div>
  );
}
```

---

#### D) Place panel in dashboard
Add to an existing dashboard layout (example for grid area).

```tsx
// In your dashboard page/component
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

export default function DashboardPage() {
  return (
    <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
      {/* existing cards ... */}
      <div className="md:col-span-1">
        <TopHubSignalPanel />
      </div>
    </div>
  );
}
```

---

#### E) Styling (Tailwind)
Ensure Tailwind classes are available (project already uses Tailwind). No extra config needed.

---

#### F) CI / build integration
- Ensure `npm run build:top-hub` runs before `next build` in CI.
- Commit `public/data/top-hub.json` in builds (or generate on deploy) so CDN serves it immediately.

---

### 3) Acceptance criteria
- `public/data/top-hub.json` exists after build and contains `{ slug, title, rank, edges, lastUpdated }`.
- Panel fetches `/data/top-hub.json` (CDN) with no Authorization header.
- Panel shows loading → data or fallback UI.
- No HuggingFace API calls from frontend or backend at runtime.
- Panel is visible on dashboard and links to `/knowledge-rag/hubs/MOC` (or configured path).

---

### 4) Time estimate
- Build script + package.json: 10m
- API route (optional): 10m
- Component + placement: 30
