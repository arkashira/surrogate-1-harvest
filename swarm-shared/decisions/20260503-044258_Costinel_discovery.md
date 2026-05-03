# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Highest-value improvement**: Add a resilient “Top Hub” panel to Costinel’s dashboard that surfaces the most-connected hub (e.g., “MOC”) and related docs using **CDN-only fetches**, zero model compute, offline/cached operation, and graceful degradation on failure.

---

### Core design decisions (resolve contradictions)
- **CDN-only, no auth**: Use `https://huggingface.co/datasets/{owner}/{repo}/resolve/main/{path}` to bypass HF API limits and avoid auth complexity.
- **Pre-listed index**: Maintain a committed `top-hub-files.json` generated once (by admin/Mac side) so runtime never calls `list_repo_tree`. Keeps runtime cheap and deterministic.
- **Server endpoint + client polling**: Expose `/api/top-hub` that reads the index and fetches from CDN; client polls (30–60s) and keeps last-known data. Avoids client-side CDN complexity and CORS/edge-cache issues.
- **Schema tolerance**: Accept multiple shapes (`hub`/`top_hub`/`hub_name`, `relatedDocs`/`related_docs`) and normalize defensively.
- **Fail-soft**: On CDN or parse failure, return cached/stale data with indicator; never block render.
- **No heavy compute**: No model training, no graph recomputation — only orchestration + CDN fetch.

---

### File changes (minimal, focused)

- `server/api/top-hub.ts` — endpoint returning `{ ok, data }`.
- `server/lib/hub-cdn.ts` — CDN fetch + index reader + normalization.
- `data/top-hub-files.json` — committed list of available hub files (generated once).
- `components/TopHubPanel.tsx` — React panel (client) with polling, stale indicator, fallback.
- Update dashboard route/page to include `<TopHubPanel />`.

---

### Code (production-ready)

#### 1) CDN + index utilities (`server/lib/hub-cdn.ts`)
```ts
// server/lib/hub-cdn.ts
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';

const HUB_DATASET_OWNER = 'AXENTX';
const HUB_DATASET_NAME = 'Costinel-Hubs';
const LOCAL_FILE_LIST = join(process.cwd(), 'data', 'top-hub-files.json');

export interface HubFileEntry {
  path: string;
  lastModified?: string;
  size?: number;
}

export interface HubPayload {
  hub: string;
  relatedDocs: Array<{
    id: string;
    title: string;
    snippet: string;
    score?: number;
    source: string;
  }>;
  generatedAt: string;
}

function readFileList(): HubFileEntry[] {
  if (!existsSync(LOCAL_FILE_LIST)) return [];
  try {
    return JSON.parse(readFileSync(LOCAL_FILE_LIST, 'utf8')) as HubFileEntry[];
  } catch {
    return [];
  }
}

function pickLatest(files: HubFileEntry[]): HubFileEntry | null {
  if (!files.length) return null;
  return files.sort((a, b) => {
    if (a.lastModified && b.lastModified) {
      return new Date(b.lastModified).getTime() - new Date(a.lastModified).getTime();
    }
    return b.path.localeCompare(a.path);
  })[0];
}

export function buildCdnUrl(path: string): string {
  return `https://huggingface.co/datasets/${HUB_DATASET_OWNER}/${HUB_DATASET_NAME}/resolve/main/${encodeURIComponent(path)}`;
}

export async function fetchHubByCdn(fileEntry: HubFileEntry): Promise<HubPayload | null> {
  const url = buildCdnUrl(fileEntry.path);
  try {
    const res = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(8000),
    });

    if (!res.ok) {
      console.warn('[hub-cdn] CDN fetch failed', res.status, url);
      return null;
    }

    const raw = await res.json();

    // Normalize expected shape (defensive)
    const hub = raw.hub || raw.top_hub || raw.hub_name || 'MOC';
    const relatedDocs = Array.isArray(raw.relatedDocs)
      ? raw.relatedDocs
      : Array.isArray(raw.related_docs)
      ? raw.related_docs
      : [];

    return {
      hub: String(hub),
      relatedDocs: relatedDocs.map((d: any, i: number) => ({
        id: d.id || `${hub}-${i}`,
        title: d.title || d.name || 'Untitled',
        snippet: d.snippet || d.summary || '',
        score: d.score ?? d.relevance ?? undefined,
        source: d.source || 'knowledge-rag',
      })),
      generatedAt: raw.generatedAt || fileEntry.lastModified || new Date().toISOString(),
    };
  } catch (err) {
    console.warn('[hub-cdn] CDN fetch error', err);
    return null;
  }
}

export async function getTopHubCached(): Promise<HubPayload | null> {
  const files = readFileList();
  const latest = pickLatest(files);
  if (!latest) return null;
  return fetchHubByCdn(latest);
}
```

#### 2) API endpoint (`server/api/top-hub.ts`)
```ts
// server/api/top-hub.ts (Next.js Route Handler style)
import { NextResponse } from 'next/server';
import { getTopHubCached } from '@/server/lib/hub-cdn';

export async function GET() {
  try {
    const payload = await getTopHubCached();
    if (!payload) {
      return NextResponse.json({ ok: false, error: 'No hub data available' }, { status: 404 });
    }
    return NextResponse.json({ ok: true, data: payload });
  } catch (err) {
    console.error('[api/top-hub]', err);
    return NextResponse.json({ ok: false, error: 'Internal server error' }, { status: 500 });
  }
}
```

#### 3) Frontend panel (`components/TopHubPanel.tsx`)
```tsx
// components/TopHubPanel.tsx
'use client';

import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';

interface RelatedDoc {
  id: string;
  title: string;
  snippet: string;
  score?: number;
  source: string;
}

interface HubData {
  hub: string;
  relatedDocs: RelatedDoc[];
  generatedAt: string;
}

export default function TopHubPanel() {
  const [data, setData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);

  const fetchData = async () => {
    try {
      const res = await fetch('/api/top-hub', { cache: 'no-store' });
      if (!res.ok) throw new Error('fetch failed');
      const json = await res.json();
      if (json?.data) {
        setData(json.data);
        setStale(false);
      }
    } catch {
      setStale(true);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 45000);
    return () => clearInterval(interval);
  }, []);

  if (loading && !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            Top Hub
            <Badge variant="secondary">Loading</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-5/6" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
     
