# Costinel / frontend

## Final Synthesis — CDN-First Top-Hub Signal Panel (Sense + Signal only)

**Goal**: Ship a resilient “Top Hub” signal panel into Costinel that shows the most-connected hub (e.g., “MOC”) and related docs using CDN-fetched artifacts.  
**Principles**: Sense + Signal. **No Execute**. Zero runtime HF API calls. No new infra, no secrets, no DB migrations.

---

### Architecture (correct + actionable)

1) **Offline/Mac orchestration** (run once or on schedule)  
   - Use local filesystem (not HF API at runtime).  
   - Walk `knowledge-rag/hubs/` (non-recursive for hubs) and parse link graph (`graph-links.json` or `[[...]]` in markdown).  
   - Compute in-degree (incoming references) per hub.  
   - Emit two static JSON files:  
     - `knowledge-rag/top-hub.json` → `{ name, path, description?, inDegree }`  
     - `knowledge-rag/top-hub-docs.json` → `Array<{ title, slug, summary, tags }>` (top 3–5)  
   - Commit these to repo (or upload to CDN path) so frontend fetches via public CDN URL:  
     `https://huggingface.co/datasets/{owner}/{repo}/resolve/main/knowledge-rag/top-hub.json`  

2) **Frontend** (Costinel)  
   - Add `TopHubSignalPanel` component.  
   - Fetch CDN JSON at mount with SWR (stale-while-revalidate).  
   - Render compact card: hub name, short description, top docs with links.  
   - Graceful fallback: if CDN 404 or fails, render minimal inline message (no crash).  
   - No API keys, no backend changes, no DB migrations.

3) **Caching & resilience**  
   - CDN URLs bypass HF API rate limits.  
   - Commit-backed artifacts ensure availability even during HF incidents.  
   - Frontend uses SWR + local fallback to avoid blocking UI.

---

### Files to add/modify

- `src/components/TopHubSignalPanel.tsx` (new)  
- `src/pages/Dashboard.tsx` (import and mount panel in sidebar/top section)  
- `src/lib/cdn.ts` (new) — tiny typed CDN fetcher with cache fallback  
- `scripts/build-top-hub.js` (new) — offline build to generate artifacts on Mac  
- `knowledge-rag/hubs/` (existing) — expected to contain hub markdown files and link graph  

---

### Code snippets

#### 1) CDN fetcher — `src/lib/cdn.ts`

```ts
// Simple typed CDN fetcher with SWR-friendly interface and graceful fallback.
export interface TopHub {
  name: string;
  path: string;
  description?: string;
  inDegree?: number;
}

export interface TopHubDoc {
  title: string;
  slug: string;
  summary: string;
  tags: string[];
}

const CDN_ROOT = 'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main';

async function fetchJSON<T>(url: string): Promise<T | null> {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchTopHub(): Promise<TopHub | null> {
  return fetchJSON<TopHub>(`${CDN_ROOT}/knowledge-rag/top-hub.json`);
}

export async function fetchTopHubDocs(): Promise<TopHubDoc[] | null> {
  return fetchJSON<TopHubDoc[]>(`${CDN_ROOT}/knowledge-rag/top-hub-docs.json`);
}
```

#### 2) Panel component — `src/components/TopHubSignalPanel.tsx`

```tsx
'use client';
import useSWR from 'swr';
import { fetchTopHub, fetchTopHubDocs, type TopHub, type TopHubDoc } from '@/lib/cdn';

const fetcher = <T,>(fn: () => Promise<T | null>) => () => fn();

export default function TopHubSignalPanel() {
  const { data: hub, error: hubError } = useSWR('top-hub', fetcher(fetchTopHub), {
    revalidateOnMount: true,
    fallbackData: null,
  });
  const { data: docs, error: docsError } = useSWR('top-hub-docs', fetcher(fetchTopHubDocs), {
    revalidateOnMount: true,
    fallbackData: null,
  });

  const loading = !hub && !hubError;
  const failed = !loading && (!hub || !docs);

  if (loading) {
    return <div className="p-3 text-xs text-gray-500">Loading top hub…</div>;
  }

  if (failed || hubError || docsError) {
    return <div className="p-3 text-xs text-gray-400">Top hub unavailable</div>;
  }

  return (
    <div className="p-3 border rounded bg-white shadow-sm">
      <h3 className="text-sm font-semibold text-gray-800">Top Hub</h3>
      <p className="text-base font-medium text-blue-700">{hub?.name || '—'}</p>
      {hub?.description && (
        <p className="mt-1 text-xs text-gray-600 line-clamp-2">{hub.description}</p>
      )}
      {hub?.inDegree != null && (
        <p className="mt-1 text-xs text-gray-400">References: {hub.inDegree}</p>
      )}

      {docs && docs.length > 0 && (
        <ul className="mt-2 space-y-1">
          {docs.slice(0, 5).map((doc) => (
            <li key={doc.slug}>
              <a
                href={`/${doc.slug}`}
                className="text-xs text-blue-600 hover:underline line-clamp-1"
                title={doc.title}
              >
                {doc.title}
              </a>
              {doc.summary && (
                <p className="text-xs text-gray-500 line-clamp-1">{doc.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

#### 3) Build script — `scripts/build-top-hub.js`

```js
#!/usr/bin/env node
/**
 * Offline build: compute top hub + related docs and emit static JSON.
 * Run: node scripts/build-top-hub.js
 * Commits artifacts to repo (knowledge-rag/top-hub.json, top-hub-docs.json).
 *
 * Uses local filesystem only (no HF API at runtime).
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, '..');
const hubsDir = path.join(repoRoot, 'knowledge-rag', 'hubs');
const outDir = path.join(repoRoot, 'knowledge-rag');

function readJSON(p) {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
}

function parseLinksFromMD(content) {
  const wikiLinks = (content.match(/\[\[([^\]]+)\]\]/g) || []).map((m) => m.slice(2, -2));
  const mdLinks = (content.match(/\[([^\]]+)\]\(([^)]+)\)/g) || []).map((m) => {
    const paren = m.slice(m.indexOf('(') + 1, -1);
    return paren;
  });
  return [...wikiLinks, ...mdLinks];
}

function buildGraph() {
  if (!fs.existsSync(hubsDir)) {
    console.warn('hubsDir not found:', hubsDir);
    return { topHub: null, topDocs: [] };
  }

  const items = fs.readdirSync(hubsDir, { withFileTypes: true });
  const hubs = [];
  const hubByName = {};

  for (const item of items)
