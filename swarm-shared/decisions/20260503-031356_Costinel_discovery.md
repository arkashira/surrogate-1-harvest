# Costinel / discovery

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### Scope
Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel that:
- Detects and surfaces the most-connected hub (default `MOC`) from a lightweight hub-graph index
- Shows 3 contextual insights from knowledge-rag
- Uses CDN-only fetches (`https://huggingface.co/datasets/.../resolve/main/...`) — zero `/api/` calls at runtime
- Fails open (no UI breakage) if CDN unavailable
- Ships as a single React component + one small build-time index generator

### Architecture
```
/opt/axentx/Costinel/
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.tsx      # new
│   ├── lib/
│   │   └── cdn.ts                    # new: CDN fetcher with cache & timeout
│   └── data/
│       └─ generateHubIndex.ts         # new: build-time indexer (run once or in CI)
├── public/data/hubs/
│   └─ top-hub-index.json              # generated: { hub, edges, insightKeys }
└── scripts/
    └─ refresh-hub-index.sh            # optional cron: lists folder via HF API once, saves JSON
```

### Data contract (public/data/hubs/top-hub-index.json)
```json
{
  "hub": "MOC",
  "edges": ["cost-governance", "cloud-finops", "anomaly-detection"],
  "insightKeys": ["moc-2026-04-27", "ri-coverage-pattern", "governance-signals"],
  "generatedAt": "2026-05-03T03:13:13Z",
  "source": "knowledge-rag"
}
```

Insight payloads (CDN JSON files):
- `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/insights/moc-2026-04-27.json`
```json
{ "title": "MOC is most-connected hub", "summary": "...", "tags": ["#knowledge-rag","#graph","#hub"] }
```

### Implementation Steps (≤2h)

1) Create CDN fetcher (`src/lib/cdn.ts`)
```ts
const CDN_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main';
const INDEX_URL = `${CDN_BASE}/indexes/top-hub-index.json`;

export async function fetchJson<T>(url: string, timeoutMs = 4000): Promise<T | null> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    clearTimeout(id);
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function loadTopHubIndex() {
  return fetchJson<{
    hub: string;
    edges: string[];
    insightKeys: string[];
    generatedAt: string;
  }>(INDEX_URL);
}

export async function loadInsight(key: string) {
  return fetchJson<{ title: string; summary: string; tags?: string[] }>(
    `${CDN_BASE}/insights/${key}.json`
  );
}
```

2) Create TopHubSignalPanel (`src/components/TopHubSignalPanel.tsx`)
```tsx
'use client';
import { useEffect, useState } from 'react';
import { loadTopHubIndex, loadInsight } from '@/lib/cdn';

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<string | null>(null);
  const [insights, setInsights] = useState<Array<{ title: string; summary: string; tags?: string[] }>>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    loadTopHubIndex()
      .then((idx) => {
        if (!mounted || !idx) return;
        setHub(idx.hub);
        // Load up to 3 insights in parallel
        return Promise.all(idx.insightKeys.slice(0, 3).map(loadInsight));
      })
      .then((results) => {
        if (!mounted) return;
        setInsights(results.filter(Boolean) as any);
      })
      .catch(() => {
        // fail open
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-4">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-300" />
        <div className="mt-3 space-y-2">
          <div className="h-4 w-full animate-pulse rounded bg-gray-200" />
          <div className="h-4 w-5/6 animate-pulse rounded bg-gray-200" />
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Top Hub Signal</h3>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700">
          {hub || 'MOC'}
        </span>
      </div>

      {insights.length === 0 && (
        <p className="mt-2 text-sm text-gray-500">No contextual insights available.</p>
      )}

      <div className="mt-3 space-y-3">
        {insights.map((insight, i) => (
          <div key={i} className="rounded-md border border-gray-100 bg-gray-50 p-3">
            <p className="text-xs font-medium text-gray-900">{insight.title}</p>
            <p className="mt-1 text-xs text-gray-600">{insight.summary}</p>
            {insight.tags && insight.tags.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {insight.tags.map((t) => (
                  <span
                    key={t}
                    className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-600"
                  >
                    {t}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <p className="mt-3 text-[10px] text-gray-400">
        Source: knowledge-rag (CDN) • Sense + Signal — ไม่ Execute
      </p>
    </div>
  );
}
```

3) Build-time index generator (`src/data/generateHubIndex.ts`)
```ts
#!/usr/bin/env tsx
import { writeFileSync, mkdirSync } from 'fs';
import { resolve } from 'path';

// Lightweight mapping; can be extended or generated from HF list_repo_tree once per day on Mac
const INDEX = {
  hub: 'MOC',
  edges: ['cost-governance', 'cloud-finops', 'anomaly-detection'],
  insightKeys: ['moc-2026-04-27', 'ri-coverage-pattern', 'governance-signals'],
  generatedAt: new Date().toISOString(),
  source: 'knowledge-rag',
};

const outDir = resolve(process.cwd(), 'public/data/hubs');
mkdirSync(outDir, { recursive: true });
writeFileSync(resolve(outDir, 'top-hub-index.json'), JSON.stringify(INDEX, null, 2));
console.log('Generated top-hub-index.json');
```
Make executable and run once (or wire into CI):
```bash
chmod +x src/data/generateHubIndex.ts
npx tsx src/data
