# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build/deploy time (zero HF API calls at runtime).

### Why this ships in <2h
- No new backend services or auth flows.
- Static JSON baked into build; runtime fetch from CDN (public, no auth).
- Reuses existing frontend patterns (cards, badges, skeleton states).
- Incremental: panel can be feature-flagged and expanded later.

---

### 1) Data contract (CDN)

File: `knowledge-rag/top-hub/latest.json` (deployed to CDN)

```json
{
  "hub": "MOC",
  "score": 0.94,
  "label": "Most-Connected Operational Model Catalog",
  "insight": "MOC shows strongest cross-project signal propagation this cycle. Prioritize governance reviews that touch MOC-adjacent services.",
  "tags": ["knowledge-rag", "graph", "hub"],
  "generatedAt": "2026-05-03T04:06:06Z",
  "ttlHours": 24
}
```

- Hosted at: `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/latest.json`
- No Authorization header required (CDN bypass).

---

### 2) Build/deploy step (one-time)

Add to CI/CD (or run manually) after `knowledge-rag` produces top-hub:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Produce top-hub artifact (example)
# ./scripts/knowledge-rag-top-hub.sh > /tmp/top-hub.json

# Validate minimal shape
jq '{hub,score,label,insight,tags,generatedAt,ttlHours}' /tmp/top-hub.json > /tmp/latest.json

# Upload to HF CDN via git-lfs or gh + git push (or HF API with token)
# For simplest CDN-only update, commit to data repo and push:
git -C /opt/axentx/data/knowledge-rag add top-hub/latest.json
git -C /opt/axentx/data/knowledge-rag commit -m "chore: update top-hub panel data"
git -C /opt/axentx/data/knowledge-rag push
```

(If HF repo is used, the `resolve/main/` URL serves the file with CDN cache.)

---

### 3) Frontend changes

#### 3.1 Types

`src/types/topHub.ts`

```ts
export interface TopHubPayload {
  hub: string;
  score: number;
  label: string;
  insight: string;
  tags: string[];
  generatedAt: string;
  ttlHours: number;
}
```

---

#### 3.2 CDN fetcher (zero runtime HF API)

`src/lib/topHub.ts`

```ts
import { TopHubPayload } from '@/types/topHub';

const TOP_HUB_CDN =
  'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/latest.json';

export async function fetchTopHub(): Promise<TopHubPayload | null> {
  try {
    const res = await fetch(TOP_HUB_CDN, {
      cache: 'no-store',
      // CDN public; no Authorization required
    });

    if (!res.ok) {
      console.warn('[TopHub] CDN fetch failed', res.status);
      return null;
    }

    const data = (await res.json()) as TopHubPayload;

    // Basic validation
    if (!data.hub || typeof data.score !== 'number') {
      console.warn('[TopHub] Invalid payload shape', data);
      return null;
    }

    return data;
  } catch (err) {
    console.warn('[TopHub] Fetch error', err);
    return null;
  }
}
```

---

#### 3.3 Panel component

`src/components/TopHubPanel/TopHubPanel.tsx`

```tsx
'use client';

import { useEffect, useState } from 'react';
import { fetchTopHub } from '@/lib/topHub';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { ExternalLink } from 'lucide-react';

export function TopHubPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    fetchTopHub()
      .then((res) => {
        if (mounted) setData(res);
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
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-6 w-20" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-5/6" />
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    // Non-blocking: silently hide if unavailable
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-sm font-semibold">
          <span>Top-Hub Signal</span>
          <Badge variant="outline" className="text-xs">
            {data.tags?.[0] ?? 'knowledge-rag'}
          </Badge>
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold tracking-tight">{data.hub}</span>
          <span className="text-sm text-muted-foreground">
            {Math.round(data.score * 100)}% relevance
          </span>
        </div>

        <p className="text-sm font-medium text-foreground">{data.label}</p>

        <p className="text-sm text-muted-foreground leading-relaxed">
          {data.insight}
        </p>

        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          <span>Updated {new Date(data.generatedAt).toLocaleDateString()}</span>
          <ExternalLink className="ml-auto h-3 w-3" />
        </div>
      </CardContent>
    </Card>
  );
}
```

---

#### 3.4 Placement (example)

Add to dashboard layout, e.g. `src/app/dashboard/page.tsx` or sidebar:

```tsx
import { TopHubPanel } from '@/components/TopHubPanel/TopHubPanel';

export default function DashboardPage() {
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="lg:col-span-2">
        {/* existing cost panels */}
      </div>

      <aside className="space-y-4">
        <TopHubPanel />
        {/* other signals */}
      </aside>
    </div>
  );
}
```

---

### 4) Runtime behavior & resilience

- Non-blocking: If CDN fetch fails or payload invalid, panel renders nothing (no errors).
- Cache: `cache: 'no-store'` ensures fresh data; CDN cache headers still apply.
- TTL: UI can optionally show `ttlHours` to indicate freshness.

---

### 5) Validation checklist (ship in <2h)

- [x] Types defined (`TopHubPayload`)
- [x] CDN fetcher with graceful failure
- [x] Panel component with loading/error states
- [x] No HF API auth or client secrets in frontend
- [x] Component placed in dashboard layout
- [
