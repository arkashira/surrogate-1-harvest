# Costinel / backend

## Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. Uses CDN-first data path (no HF API during render) and reuses existing Lightning Studio quota patterns for any downstream compute.

**Time budget**: <2h  
**Files to touch**:
- `src/components/dashboard/TopHubSignalPanel.tsx` (new)
- `src/api/graph.ts` (new lightweight CDN fetcher)
- `src/types/graph.ts` (new minimal types)
- `src/components/dashboard/index.ts` (export)
- `src/pages/Dashboard.tsx` (mount panel)

---

### 1) Types (`src/types/graph.ts`)

```ts
// Minimal shape for top-hub signal payload (projected at build/ingest time)
export interface KnowledgeHub {
  id: string;        // e.g. "MOC"
  label: string;     // human readable
  rank: number;      // connection strength / signal score
  description?: string;
}

export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  effort: 'low' | 'medium' | 'high';
  tags: string[];
  href?: string;     // optional deep link
}

export interface TopHubSignal {
  hub: KnowledgeHub;
  proposals: Proposal[];
  generatedAt: string; // ISO
  file: string;        // provenance (e.g. batches/mirror-merged/2026-05-03/slug.json)
}
```

---

### 2) CDN Fetcher (`src/api/graph.ts`)

```ts
import { TopHubSignal } from '../types/graph';

const CDN_ROOT = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main';

export async function fetchTopHubSignal(
  dateFolder = 'latest',
  fileName = 'top-hub-signal.json'
): Promise<TopHubSignal> {
  // Build path once on orchestration side (Mac) and embed or pass via env.
  // Default fallback uses latest folder to avoid frequent API calls.
  const url = `${CDN_ROOT}/${dateFolder}/${fileName}`;
  const res = await fetch(url, { cache: 'no-store' });

  if (!res.ok) {
    // Graceful fallback: return a minimal local stub so UI never breaks
    console.warn(`[graph] CDN fetch failed ${res.status} for ${url}`);
    return {
      hub: { id: 'MOC', label: 'MOC', rank: 0, description: 'Data unavailable' },
      proposals: [],
      generatedAt: new Date().toISOString(),
      file: `${dateFolder}/${fileName}`
    };
  }

  return res.json();
}
```

---

### 3) Panel Component (`src/components/dashboard/TopHubSignalPanel.tsx`)

```tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHubSignal, type TopHubSignal } from '../../api/graph';
import { Card, CardHeader, CardTitle, CardContent } from '../ui/card';
import { Badge } from '../ui/badge';
import { ExternalLink } from 'lucide-react';

const impactColor = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-emerald-100 text-emerald-800'
} as const;

export const TopHubSignalPanel: React.FC = () => {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubSignal()
      .then((data) => {
        if (mounted) setSignal(data);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => { mounted = false; };
  }, []);

  if (loading) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="animate-pulse space-y-3">
            <div className="h-5 w-32 bg-slate-200 rounded" />
            <div className="h-4 w-full bg-slate-100 rounded" />
            <div className="h-4 w-5/6 bg-slate-100 rounded" />
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!signal || signal.proposals.length === 0) {
    return (
      <Card>
        <CardContent className="p-6 text-center text-slate-500">
          No actionable signals available at the moment.
        </CardContent>
      </Card>
    );
  }

  const { hub, proposals } = signal;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-base">
          <span>Top Signal — {hub.label}</span>
          <Badge variant="outline" className="text-xs">
            Rank {hub.rank}
          </Badge>
        </CardTitle>
        {hub.description && (
          <p className="text-sm text-slate-500 mt-1">{hub.description}</p>
        )}
      </CardHeader>

      <CardContent>
        <ul className="space-y-3" role="list">
          {proposals.slice(0, 3).map((p) => (
            <li key={p.id} className="border rounded-md p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <h4 className="font-medium text-sm text-slate-900 truncate">
                    {p.title}
                  </h4>
                  <p className="text-xs text-slate-500 mt-1 line-clamp-2">
                    {p.summary}
                  </p>
                  <div className="flex flex-wrap gap-1 mt-2">
                    <Badge variant="secondary" className={`${impactColor[p.impact]} text-xs`}>
                      {p.impact} impact
                    </Badge>
                    <Badge variant="outline" className="text-xs">
                      {p.effort} effort
                    </Badge>
                    {p.tags.slice(0, 2).map((t) => (
                      <Badge key={t} variant="ghost" className="text-xs">
                        {t}
                      </Badge>
                    ))}
                  </div>
                </div>
                {p.href && (
                  <a
                    href={p.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-slate-400 hover:text-slate-600 flex-shrink-0"
                    aria-label={`Open ${p.title}`}
                  >
                    <ExternalLink className="w-4 h-4" />
                  </a>
                )}
              </div>
            </li>
          ))}
        </ul>

        <p className="text-xs text-slate-400 mt-3">
          Generated {new Date(signal.generatedAt).toLocaleString()}
          {signal.file && ` — ${signal.file}`}
        </p>
      </Card>
    </Card>
  );
};
```

---

### 4) Exports (`src/components/dashboard/index.ts`)

```ts
export { TopHubSignalPanel } from './TopHubSignalPanel';
```

---

### 5) Mount on Dashboard (`src/pages/Dashboard.tsx`)

Locate the main dashboard grid and insert near the top (after header or in a prominent sidebar/top-row slot):

```tsx
import { TopHubSignalPanel } from '../components/dashboard';

// Inside your Dashboard component render:
{/* Top Signal Row */}
<div className="mb-6">
  <TopHubSignalPanel />
</div>

{/* Rest of existing dashboard cards... */}
```

---

### 6) Build
