# Costinel / frontend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked at build time and fetched from CDN; panel is opt-in and non-blocking.

---

### Architecture (CDN-first)

1. **Mac orchestration** (run occasionally):
   - `list_repo_tree` once per date folder → save `top-hub.json`
   - Upload to repo as `public/signals/top-hub.json` (committed or via CDN raw path)
2. **Frontend** (Costinel):
   - Fetch `https://huggingface.co/datasets/.../resolve/main/public/signals/top-hub.json` (CDN, no auth, no rate limit)
   - Render small card: hub name, connection count, last updated
   - Fail silently (no blocking UI)
3. **Zero runtime API calls** — only CDN GET.

---

### Implementation Steps (≤2h)

1. Add `TopHubSignalPanel` React component (TypeScript)
2. Add fetch hook with CDN URL + stale-while-revalidate + 5m cache
3. Place panel in dashboard sidebar/header (non-blocking)
4. Add build script stub (`scripts/fetch-top-hub.sh`) for Mac orchestration (optional commit)
5. Update README section about signals (one line)

---

### Code Snippets

#### 1) Component: `src/components/TopHubSignalPanel.tsx`

```tsx
import { useEffect, useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ExternalLink, TrendingUp } from "lucide-react";

interface TopHubPayload {
  hub: string;
  connections: number;
  updated_at: string; // ISO
  source_doc?: string;
}

const CDN_TOP_HUB =
  "https://huggingface.co/datasets/AXENTX/Signals/resolve/main/public/signals/top-hub.json";

export function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<boolean>(false);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(CDN_TOP_HUB, { cache: "no-store" });
      if (!res.ok) throw new Error("CDN fetch failed");
      const json = (await res.json()) as TopHubPayload;
      setData(json);
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    // refresh every 5m in background (non-blocking)
    const id = setInterval(fetchData, 5 * 60 * 1000);
    return () => clearInterval(id);
  }, [fetchData]);

  if (loading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <TrendingUp className="h-4 w-4 text-muted-foreground" />
            Top Hub Signal
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-10 w-full rounded" />
        </CardContent>
      </Card>
    );
  }

  if (error || !data) {
    // Non-blocking: render nothing on failure
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-sm font-medium">
          <span className="flex items-center gap-2">
            <TrendingUp className="h-4 w-4 text-muted-foreground" />
            Top Hub Signal
          </span>
          {data.source_doc && (
            <a
              href={data.source_doc}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold tabular-nums">{data.hub}</span>
          <span className="text-sm text-muted-foreground">
            {data.connections.toLocaleString()} connections
          </span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Updated {new Date(data.updated_at).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </p>
      </Card>
    </Card>
  );
}
```

#### 2) Hook (optional): `src/hooks/useTopHubSignal.ts`

```ts
import useSWR from "swr";

const CDN_TOP_HUB =
  "https://huggingface.co/datasets/AXENTX/Signals/resolve/main/public/signals/top-hub.json";

const fetcher = (url: string) =>
  fetch(url, { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error("CDN fetch failed");
    return r.json();
  });

export function useTopHubSignal() {
  const { data, error, isLoading, mutate } = useSWR<{
    hub: string;
    connections: number;
    updated_at: string;
    source_doc?: string;
  }>(CDN_TOP_HUB, fetcher, {
    revalidateOnFocus: false,
    refreshInterval: 5 * 60 * 1000,
    shouldRetryOnError: false,
  });

  return {
    data,
    error,
    isLoading,
    refresh: mutate,
  };
}
```

#### 3) Place in dashboard (example: `src/pages/dashboard.tsx`)

```tsx
import { TopHubSignalPanel } from "@/components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div className="grid gap-6">
      {/* existing header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Cost Dashboard</h1>
          <p className="text-sm text-muted-foreground">
            Real-time multi-cloud cost visibility
          </p>
        </div>

        {/* Top Hub Signal — non-blocking */}
        <div className="w-64 shrink-0">
          <TopHubSignalPanel />
        </div>
      </div>

      {/* rest of dashboard */}
    </div>
  );
}
```

#### 4) Build-time helper (Mac orchestration) — `scripts/fetch-top-hub.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/fetch-top-hub.sh <date-folder>
# Example: ./scripts/fetch-top-hub.sh 2026-05-03

DATE_FOLDER="${1:-$(date +%Y-%m-%d)}"
REPO="AXENTX/Signals"
OUT="public/signals/top-hub.json"

echo "Fetching top hub for ${DATE_FOLDER}..."

# Single API call to list folder (non-recursive)
FILES=$(python3 -c "
import os, json, sys
from huggingface_hub import list_repo_tree
tree = list_repo_tree('$REPO', path='$DATE_FOLDER', recursive=False)
print(json.dumps([f.rfilename for f in tree if f.rfilename.endswith('.json')]))
" 2>/dev/null || echo "[]")

# Pick most recent top-hub file if exists, else stub
TOP_HUB_FILE=$(echo "$FILES" | python3 -
