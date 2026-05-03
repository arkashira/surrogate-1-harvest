# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HF API calls at runtime).

---

### 1) Build-time data pipeline (Mac/CI) — 15 min
Create `scripts/build-top-hub.js` (or `.ts` if project uses ts).  
- Uses single `list_repo_tree(path, recursive=False)` call for `knowledge-rag/hubs/` (one folder).  
- Computes most-connected hub by parsing `hub-{slug}.json` files (each contains `connections: []`).  
- Outputs `/public/data/top-hub.json` (committed or baked into Docker image).  
- Fallback: if API 429 or offline, reuse last known file (git-tracked).

```js
// scripts/build-top-hub.js
#!/usr/bin/env node
import { writeFileSync, mkdirSync } from 'fs';
import { join } from 'path';
import { HfApi } from '@huggingface/hub';

const HUB_REPO = 'AXENTX/knowledge-rag'; // adjust if different
const HUB_PATH = 'hubs';
const OUT_DIR = 'public/data';
const OUT_FILE = 'top-hub.json';

async function main() {
  const api = new HfApi();
  try {
    // Single non-recursive tree call (fast, low quota)
    const tree = await api.listRepoTree({ repo: HUB_REPO, path: HUB_PATH, recursive: false });
    const files = tree.filter((t) => t.type === 'file' && t.path.endsWith('.json'));

    let best = null;
    let bestCount = -1;

    for (const f of files) {
      // CDN fetch — no Authorization header, bypasses /api/ rate limits
      const res = await fetch(
        `https://huggingface.co/datasets/${HUB_REPO}/resolve/main/${f.path}`
      );
      if (!res.ok) continue;
      const hub = await res.json();
      const count = Array.isArray(hub.connections) ? hub.connections.length : 0;
      if (count > bestCount) {
        bestCount = count;
        best = { slug: hub.slug || f.path.replace(/^.*[\\/]/, '').replace('.json', ''), name: hub.name || hub.slug, count, ...hub };
      }
    }

    const payload = best || { slug: 'MOC', name: 'MOC', count: 0, note: 'fallback' };
    mkdirSync(OUT_DIR, { recursive: true });
    writeFileSync(join(OUT_DIR, OUT_FILE), JSON.stringify(payload, null, 2), 'utf8');
    console.log('Top-hub baked:', payload);
  } catch (err) {
    console.warn('Build-time top-hub failed, using fallback:', err.message);
    // ensure fallback exists
    mkdirSync(OUT_DIR, { recursive: true });
    writeFileSync(join(OUT_DIR, OUT_FILE), JSON.stringify({ slug: 'MOC', name: 'MOC', count: 0, note: 'fallback' }), 'utf8');
  }
}

main();
```

Add to `package.json` scripts:
```json
"scripts": {
  "build:top-hub": "node scripts/build-top-hub.js",
  "prebuild": "npm run build:top-hub"
}
```

---

### 2) Frontend panel component — 45 min
Create `components/TopHubSignalPanel.tsx` (or `.jsx`).  
- Loads `/data/top-hub.json` at runtime (static fetch, no HF API).  
- Non-blocking: lazy-load or low-priority fetch; skeleton while loading.  
- Shows hub name, connection count, short insight, and link to hub detail.

```tsx
// components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface HubData {
  slug: string;
  name: string;
  count: number;
  insight?: string;
  note?: string;
}

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Low priority: requestIdleCallback or setTimeout to avoid blocking paint
    const timer = setTimeout(async () => {
      try {
        const res = await fetch('/data/top-hub.json', { cache: 'force-cache' });
        if (res.ok) {
          const data = await res.json();
          setHub(data);
        }
      } catch {
        // ignore
      } finally {
        setLoading(false);
      }
    }, 1200);

    return () => clearTimeout(timer);
  }, []);

  if (loading && !hub) {
    return (
      <div className="top-hub-panel skeleton" aria-hidden="true">
        <div className="skeleton-line" />
        <div className="skeleton-line short" />
      </div>
    );
  }

  if (!hub) return null;

  return (
    <div className="top-hub-panel" role="region" aria-label="Top hub signal">
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <span className="top-hub-name">{hub.name}</span>
      </div>
      <div className="top-hub-body">
        <p className="top-hub-count">{hub.count} connections</p>
        {hub.insight && <p className="top-hub-insight">{hub.insight}</p>}
        <a
          className="top-hub-link"
          href={`/knowledge-rag/hubs/${hub.slug}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          View hub details →
        </a>
      </div>
    </div>
  );
}
```

Basic styles (`components/TopHubSignalPanel.css`):
```css
.top-hub-panel {
  border: 1px solid #e6eef8;
  background: #fbfdff;
  border-radius: 10px;
  padding: 14px 16px;
  max-width: 320px;
  font-family: system-ui, -apple-system, sans-serif;
}
.top-hub-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.top-hub-badge {
  font-size: 11px;
  font-weight: 600;
  color: #0b74de;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.top-hub-name {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}
.top-hub-count {
  margin: 4px 0 8px;
  font-size: 22px;
  font-weight: 700;
  color: #0ea5a4;
}
.top-hub-insight {
  margin: 0 0 8px;
  font-size: 13px;
  color: #475569;
}
.top-hub-link {
  font-size: 13px;
  color: #0b74de;
  text-decoration: none;
}
.top-hub-link:hover {
  text-decoration: underline;
}

/* skeleton */
.skeleton {
  background: #f8fafc;
  border-radius: 10px;
  padding: 14px 16px;
  max-width: 320px;
}
.skeleton-line {
  height: 12px;
  background: #eef2f7;
  border-radius: 4px;
  margin-bottom: 8px;
}
.skeleton-line.short {
  width: 60%;
}
```

---

### 3) Integrate into dashboard — 20 min
Place panel in the cost dashboard sidebar/header area (e.g., near cost alerts
