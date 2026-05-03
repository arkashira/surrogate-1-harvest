# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a lightweight “Top-Hub Insight” panel to the Costinel ops dashboard that surfaces the most-connected knowledge-rag hub (e.g., MOC) with **zero runtime HF API calls**, using CDN-only fetches and a build-time baked file list.

---

### Scope (what we ship)
- Add `TopHubSignal` React component at `/src/components/TopHubSignal.tsx`
- Add CDN fetch utility at `/src/lib/cdn.ts` (no Authorization header)
- Add build script at `/scripts/build-top-hub-index.js` (run on Mac orchestration)
- Add generated file `/public/data/hub-list.json` (committed to repo)
- Mount panel in ops dashboard (e.g., `/ops` route or sidebar)
- No runtime HF API usage in browser; only CDN GETs

---

### Step-by-step implementation

#### 1) Build script: `build-top-hub-index.js` (run on Mac orchestration)

```js
// scripts/build-top-hub-index.js
// Run on Mac (or CI) after HF rate-limit window clears.
// Uses REST to list_repo_tree once and saves hub-list.json to public/data.
// Requires HUGGING_FACE_HUB_TOKEN in env for authenticated tree listing.

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const REPO = 'AXENTX/knowledge-rag'; // adjust if needed
const FOLDER = 'top-hubs'; // folder containing hub files
const OUT_DIR = path.join(__dirname, '..', 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'hub-list.json');

async function listTree() {
  const token = process.env.HUGGING_FACE_HUB_TOKEN;
  if (!token) {
    console.warn('HUGGING_FACE_HUB_TOKEN not set; using unauthenticated tree (may fail).');
  }

  // list_repo_tree(path, recursive=False) equivalent via REST
  const url = `https://huggingface.co/api/models/${REPO}/tree?path=${encodeURIComponent(FOLDER)}&recursive=false`;
  const res = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {}
  });

  if (!res.ok) {
    throw new Error(`Failed to list tree: ${res.status} ${await res.text()}`);
  }

  const entries = await res.json();

  // Filter only files we care about (e.g., .json or .md)
  const files = entries
    .filter(e => e.type === 'file' && /\.(json|md)$/i.test(e.name))
    .map(e => ({
      path: `${FOLDER}/${e.name}`,
      name: e.name,
      // CDN URL (no auth, no API)
      cdn_url: `https://huggingface.co/datasets/${REPO}/resolve/main/${FOLDER}/${encodeURIComponent(e.name)}`
    }));

  // Pick "most-connected" by heuristic: highest in sort or named MOC.*
  const top = files.find(f => /moc/i.test(f.name)) || files[0] || null;

  const payload = {
    generated_at: new Date().toISOString(),
    repo: REPO,
    folder: FOLDER,
    top_hub: top,
    candidates: files
  };

  if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_FILE, JSON.stringify(payload, null, 2));
  console.log(`Baked hub list to ${OUT_FILE}`);
}

listTree().catch(err => {
  console.error(err);
  process.exit(1);
});
```

Make it executable and ensure it can be run via bash:

```bash
chmod +x scripts/build-top-hub-index.js
# run with:
# HUGGING_FACE_HUB_TOKEN=hf_xxx node scripts/build-top-hub-index.js
```

Add to package.json scripts (optional):

```json
"scripts": {
  "build:hub": "node scripts/build-top-hub-index.js"
}
```

---

#### 2) CDN utility (browser-safe, no auth)

```ts
// src/lib/cdn.ts
export async function fetchHubJson<T = unknown>(cdnPath: string): Promise<T> {
  // CDN URL — public, no Authorization header
  const res = await fetch(cdnPath, {
    cache: 'no-store'
  });

  if (!res.ok) {
    throw new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);
  }

  return res.json() as Promise<T>;
}
```

---

#### 3) TopHubSignal component

```tsx
// src/components/TopHubSignal.tsx
import { useEffect, useState } from 'react';
import { fetchHubJson } from '../lib/cdn';

interface HubList {
  generated_at: string;
  repo: string;
  folder: string;
  top_hub: {
    path: string;
    name: string;
    cdn_url: string;
  } | null;
  candidates: Array<{
    path: string;
    name: string;
    cdn_url: string;
  }>;
}

interface HubContent {
  title?: string;
  summary?: string;
  insights?: string[];
  last_updated?: string;
  // flexible — allow any JSON blob from hub file
  [k: string]: unknown;
}

export default function TopHubSignal() {
  const [hubList, setHubList] = useState<HubList | null>(null);
  const [content, setContent] = useState<HubContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Load baked list (static, served from public/)
    fetch('/data/hub-list.json')
      .then((r) => {
        if (!r.ok) throw new Error('Hub list not available');
        return r.json() as Promise<HubList>;
      })
      .then((list) => {
        setHubList(list);
        if (list.top_hub?.cdn_url) {
          return fetchHubJson<HubContent>(list.top_hub.cdn_url).then((c) => {
            setContent(c);
          });
        }
        return Promise.resolve();
      })
      .catch((err) => {
        console.error(err);
        setError(err.message || 'Failed to load hub signal');
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="rounded border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600">
        Loading top-hub signal...
      </div>
    );
  }

  if (error || !hubList?.top_hub) {
    return (
      <div className="rounded border border-yellow-100 bg-yellow-50 p-4 text-sm text-yellow-800">
        Top-hub signal unavailable.
      </div>
    );
  }

  const title = content?.title || hubList.top_hub.name.replace(/\.[^/.]+$/, '');
  const summary = content?.summary || 'No summary available.';
  const insights = Array.isArray(content?.insights) ? content.insights : [];

  return (
    <div className="rounded border border-blue-200 bg-blue-50 p-4">
      <div className="mb-2 flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold text-blue-900">Top Hub: {title}</h3>
          <p className="text-xs text-blue-700">{hubList.top_hub.name}</p>
        </div>
      </div>

      <p className="mb-3 text-sm text-blue-800">{summary}</p>

      {insights.length > 0 && (
        <ul className="space-y-1">
          {insights.map((insight, i) => (
            <li key={i} className="text-xs text-blue-700
