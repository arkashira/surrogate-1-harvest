# Costinel / discovery

## Final Consolidated Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Ship a resilient “Top Hub” signal panel into Costinel that shows the most-connected hub (e.g., “MOC”) and related docs using CDN-fetched artifacts (zero runtime HF API calls).  
**Non-negotiable principle**: All runtime fetches are CDN-only; no HuggingFace API calls in production; cacheable; degrades gracefully.

---

### 1) Scope (what we ship)
- Add `/signals/top-hub` (component + optional `/api/top-hub` endpoint) that:
  - Uses a pre-generated `top-hub.json` artifact built at build time.
  - Picks top hub by `hub_score` (or `connections`/`pagerank`).
  - Shows related docs by `related_ids` or tag overlap.
- Build step generates `top-hub.json` from knowledge-rag repo using CDN fetches only.
- Runtime fetches: static JSON + CDN doc paths only.
- Client-side cache (5–10 min TTL) + HTTP cache headers.
- Fallback UI when data unavailable.

---

### 2) File layout (concrete)

```
Costinel/
├── public/
│   └── data/
│       └── top-hub.json          # generated artifact (committed or built)
├── src/
│   ├── components/
│   │   └── TopHubPanel.tsx
│   ├── lib/
│   │   ├── signals/
│   │   │   ├── hubIndex.ts       # loader + CDN fetcher
│   │   │   └── types.ts
│   │   └── cache.ts
│   ├── pages/
│   │   └── SignalsPage.tsx       # route mount point
│   └── app/
│       └── api/
│           └── top-hub/
│               └── route.ts       # optional Next.js API route
├── scripts/
│   └── build-top-hub.js          # build script (Node)
└── package.json
```

---

### 3) Data model (canonical)

```ts
// src/lib/signals/types.ts
export interface HubDoc {
  id: string;
  slug: string;
  title: string;
  summary: string;
  tags: string[];
  hub_score: number;   // primary ranking (alias: connections OK)
  related_ids: string[];
  cdn_path: string;    // e.g. "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/docs/moc-2026-04-27.json"
  updated_at?: string;
}

// TopHubIndex is the artifact shape
export type TopHubIndex = {
  generated_at: string;
  top_hubs: HubDoc[];    // sorted desc by hub_score
};
```

---

### 4) Build script (Node) — `scripts/build-top-hub.js`

```js
// scripts/build-top-hub.js
// Run: node scripts/build-top-hub.js
// Uses only CDN fetches (no HF auth). Can be run locally or in CI.

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const OUT_DIR = path.resolve(REPO_ROOT, 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub.json');

// Configurable knowledge-rag repo location (CDN base)
const HF_DATASET_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main';
// If you prefer local tree, point to local folder instead and skip CDN fetch per file.

async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
  return res.json();
}

// Minimal discovery: either use a prebuilt index file or list known doc slugs.
// For v0, prefer explicit list or a small index file to avoid recursive tree walks.
async function build() {
  try {
    // Option A (preferred): use a lightweight index file committed to repo or generated elsewhere.
    // const index = await fetchJSON(`${HF_DATASET_BASE}/index/top-candidates.json`);

    // Option B (fallback): hardcode candidate slugs for v0 (fast, deterministic).
    const candidates = [
      'moc-2026-04-27',
      'hub-alpha-2026',
      'hub-beta-2026',
      'hub-gamma-2026',
    ];

    const docs = [];
    for (const slug of candidates) {
      const cdnPath = `${HF_DATASET_BASE}/docs/${slug}.json`;
      try {
        const raw = await fetchJSON(cdnPath);
        // Normalize to HubDoc
        const doc = {
          id: raw.id || slug,
          slug,
          title: raw.title || slug,
          summary: raw.summary || '',
          tags: raw.tags || [],
          hub_score: Number(raw.hub_score || raw.connections || 0),
          related_ids: raw.related_ids || [],
          cdn_path: cdnPath,
          updated_at: raw.updated_at || new Date().toISOString(),
        };
        docs.push(doc);
      } catch (err) {
        console.warn(`Skipping ${slug}:`, err.message);
      }
    }

    docs.sort((a, b) => b.hub_score - a.hub_score);
    const top = docs.slice(0, 5); // keep top-N

    const out = {
      generated_at: new Date().toISOString(),
      top_hubs: top,
    };

    fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(OUT_FILE, JSON.stringify(out, null, 2), 'utf8');
    console.log(`Built ${OUT_FILE} with ${top.length} hubs`);
  } catch (err) {
    console.error('Build failed:', err);
    process.exit(1);
  }
}

build();
```

**Notes**:
- Keep candidate list small for v0; expand later via an index file.
- No recursive tree listing required — avoids heavy calls and keeps runtime simple.
- Output is static JSON served from `public/data/top-hub.json`.

---

### 5) Runtime loader + cache — `src/lib/signals/hubIndex.ts`

```ts
// src/lib/signals/hubIndex.ts
import type { HubDoc, TopHubIndex } from './types';
import { SimpleCache } from '../cache';

const INDEX_URL = '/data/top-hub.json'; // static from public/
const cache = new SimpleCache<TopHubIndex>('top-hub-index', 10 * 60 * 1000);

export async function fetchTopHubIndex(): Promise<TopHubIndex | null> {
  const cached = cache.get();
  if (cached) return cached;

  try {
    const res = await fetch(INDEX_URL, { cache: 'no-store' });
    if (!res.ok) throw new Error('Failed to fetch top-hub index');
    const json: TopHubIndex = await res.json();
    cache.set(json);
    return json;
  } catch (err) {
    console.warn('[TopHub] index fetch failed', err);
    return null;
  }
}

export async function getTopHubAndRelated(topN = 1, maxRelated = 6): Promise<{
  hub: HubDoc | null;
  related: HubDoc[];
}> {
  const index = await fetchTopHubIndex();
  if (!index || !index.top_hubs.length) return { hub: null, related: [] };

  const hub = index.top_hubs[0];
  const candidates = index.top_hubs.filter((h) => h.id !== hub.id);

  const relatedSet = new Set<string>();
  hub.related_ids.forEach((id) => relatedSet.add(id));

  // fallback: tag overlap
  if (relatedSet.size === 0 && hub.tags) {
    const hubTags = new Set(hub.tags);
    candidates.forEach((doc) => {
      if (doc.tags.some((t) =>
