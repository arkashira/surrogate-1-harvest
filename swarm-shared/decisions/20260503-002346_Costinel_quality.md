# Costinel / quality

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Goal**: Ship a read-only ops dashboard card in <2h that surfaces the most-connected hub (e.g., MOC) from the existing knowledge-rag graph with actionable context.  
**Constraints met**: zero backend changes, no auth/permissions work, strictly GET/render, uses existing graph data, follows “Sense + Signal (ไม่ Execute)”.

---

## 1) Architecture (minimal, production-ready)
- **Data source**: existing knowledge-rag graph export (JSON) produced by prior runs.
- **Ingestion**: none. Runtime fetch with graceful static fallback.
- **Card responsibilities**:
  1. Identify top hub (highest degree/centrality) from graph.
  2. Show hub ID, label, score, concise summary, and top 3 related docs/nodes.
  3. Display last-updated timestamp.
- **No mutations**: strictly read-only. No API keys or auth required.

---

## 2) File structure (additions only)

```
Costinel/
└── src/
    └── components/
        └── dashboard/
            ├── TopHubSignalCard.tsx
            └── hooks/
                └── useTopHub.ts
```

Static fallback (for immediate ship):

```
public/
└── mock/
    └── top-hub.json
```

---

## 3) Data contract (reuse + normalize)

```ts
// src/types/knowledge-rag.ts
export interface TopHub {
  hubId: string;        // e.g. "MOC"
  label: string;        // e.g. "Multi-Org Cost model"
  score: number;        // centrality/connection strength [0,1]
  summary: string;      // 1–2 sentence insight
  relatedDocs: Array<{
    title: string;
    url: string;
    relevance: number;  // [0,1]
  }>;
  lastUpdated: string;  // ISO
}
```

If the live endpoint is unavailable, `useTopHub` falls back to `public/mock/top-hub.json` (ensures immediate render).

---

## 4) Hook: `useTopHub` (cached, resilient)

```ts
// src/components/dashboard/hooks/useTopHub.ts
import { useEffect, useState, useCallback } from "react";
import type { TopHub } from "@/types/knowledge-rag";

const FALLBACK_URL = "/mock/top-hub.json";

export function useTopHub(pollIntervalMs = 300_000) {
  const [topHub, setTopHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchTopHub = useCallback(async () => {
    try {
      // Try live endpoint first; tolerate network failures
      const res = await fetch("/api/knowledge-rag/top-hub", {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
      }).catch(() => null);

      const url = res?.ok ? "/api/knowledge-rag/top-hub" : FALLBACK_URL;
      const data = await fetch(url, { cache: "no-store" }).then((r) => r.json());

      // Basic normalization guard
      if (!data || typeof data.hubId !== "string") {
        throw new Error("Invalid top-hub payload");
      }

      setTopHub(data);
      setError(null);
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchTopHub();
    const id = setInterval(fetchTopHub, pollIntervalMs);
    return () => clearInterval(id);
  }, [fetchTopHub, pollIntervalMs]);

  return { topHub, loading, error, refetch: fetchTopHub };
}
```

---

## 5) Component: `TopHubSignalCard`

Uses existing design tokens and UI primitives for consistency.

```tsx
// src/components/dashboard/TopHubSignalCard.tsx
import { useTopHub } from "./hooks/useTopHub";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ExternalLink } from "lucide-react";
import type { TopHub } from "@/types/knowledge-rag";

function RelatedDocsList({ docs }: { docs: TopHub["relatedDocs"] }) {
  if (!docs?.length) return null;
  return (
    <ul className="mt-3 space-y-2">
      {docs.slice(0, 3).map((doc) => (
        <li key={doc.url} className="flex items-center gap-2 text-sm">
          <span className="truncate font-medium text-muted-foreground">
            {doc.title}
          </span>
          <a
            href={doc.url}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            title="Open related doc"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </li>
      ))}
    </ul>
  );
}

export function TopHubSignalCard() {
  const { topHub, loading, error } = useTopHub();

  return (
    <Card className="relative">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base font-semibold">
          <span className="h-2 w-2 rounded-full bg-amber-500 animate-pulse" />
          Top-Hub Signal
        </CardTitle>
      </CardHeader>

      <CardContent>
        {loading && (
          <div className="space-y-3">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        )}

        {error && (
          <p className="text-sm text-destructive">
            Unable to load top-hub signal. Showing cached insights.
          </p>
        )}

        {topHub && (
          <>
            <div className="mb-2 flex items-baseline gap-2">
              <span className="text-2xl font-bold tracking-tight">
                {topHub.hubId}
              </span>
              <span className="text-sm text-muted-foreground">
                ({topHub.label})
              </span>
            </div>

            <p className="mb-3 text-sm text-muted-foreground">
              {topHub.summary}
            </p>

            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>Strength: {Math.round(topHub.score * 100)}%</span>
              <span>Updated {new Date(topHub.lastUpdated).toLocaleDateString()}</span>
            </div>

            <RelatedDocsList docs={topHub.relatedDocs} />
          </>
        )}

        <p className="mt-4 text-[10px] uppercase tracking-wider text-muted-foreground/60">
          Sense + Signal — ไม่ Execute
        </p>
      </CardContent>
    </Card>
  );
}
```

---

## 6) Integration into ops dashboard

Add to the dashboard grid (example for `/ops` route):

```tsx
// src/pages/ops.tsx (or wherever dashboard layout lives)
import { TopHubSignalCard } from "@/components/dashboard/TopHubSignalCard";

export default function OpsDashboard() {
  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {/* existing cards ... */}
      <TopHubSignalCard />
    </div>
  );
}
```

---

## 7) Mock data for immediate ship

Create `public/mock/top-hub.json` so the card renders without backend:

```json
