# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: <2h (frontend-only, zero backend changes).  
**Assumptions**:  
- A JSON endpoint or static file at `/data/knowledge-rag/top-hubs.json` (or similar) exists or will be mocked for now.  
- The card is read-only and lives in the dashboard sidebar or a “Insights” pane.  
- Uses existing design tokens/components where possible.

### 1) Data contract (single source of truth)
Use this shape for `/data/knowledge-rag/top-hubs.json` (and the API if available):

```json
{
  "hubs": [
    {
      "id": "MOC",
      "label": "MOC",
      "connections": 142,
      "signals": [
        {
          "id": "sig-1",
          "title": "Signal title",
          "summary": "Short summary",
          "badge": "Optional",
          "source": "https://example.com"
        }
      ],
      "type": "hub",
      "description": "Optional description",
      "link": "https://example.com/hub"
    }
  ]
}
```

- **Why**: Combines Candidate 2’s explicit contract with Candidate 1’s flexibility (supports `type`, `description`, `link`).  
- **Action**: Add a static fallback at `public/data/knowledge-rag/top-hubs.json`.

---

### 2) File changes (minimal, focused)
- Add `TopHubSignalCard` at `src/components/cards/TopHubSignalCard.tsx`.  
- Add `useTopHub` hook at `src/hooks/useTopHub.ts`.  
- Add the card to the dashboard layout (sidebar or insights pane).  
- Add static fallback JSON at `public/data/knowledge-rag/top-hubs.json`.

---

### 3) Hook — `useTopHub.ts` (robust + simple)
```ts
// src/hooks/useTopHub.ts
import { useEffect, useState } from "react";

export type Signal = {
  id?: string;
  title?: string;
  summary?: string;
  badge?: string;
  source?: string;
};

export type Hub = {
  id: string;
  label?: string;
  type?: string;
  description?: string;
  link?: string;
  connections?: number;
  degree?: number;
  signals?: Signal[];
};

type HubsPayload = {
  hubs: Hub[];
};

const ENDPOINTS = [
  "/api/knowledge-rag/top-hubs",
  "/data/knowledge-rag/top-hubs.json",
] as const;

export function useTopHub() {
  const [topHub, setTopHub] = useState<Hub | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function load() {
      try {
        let payload: HubsPayload | null = null;

        // Try endpoints in order
        for (const url of ENDPOINTS) {
          try {
            const res = await fetch(url, { cache: "no-store" });
            if (res.ok) {
              payload = await res.json();
              break;
            }
          } catch {
            // continue to next endpoint
          }
        }

        if (!mounted) return;

        if (!payload || !Array.isArray(payload.hubs) || payload.hubs.length === 0) {
          setError("No hub data available");
          setTopHub(null);
          setSignals([]);
          return;
        }

        // Pick most-connected hub
        const best = payload.hubs.reduce((a, b) => {
          const aConn = Number(a.connections ?? a.degree ?? 0);
          const bConn = Number(b.connections ?? b.degree ?? 0);
          return bConn > aConn ? b : a;
        });

        setTopHub(best);
        const hubSignals = Array.isArray(best.signals) ? best.signals : [];
        setSignals(hubSignals.slice(0, 3));
      } catch (err) {
        if (mounted) {
          setError(err instanceof Error ? err.message : "Failed to load hubs");
          setTopHub(null);
          setSignals([]);
        }
      } finally {
        if (mounted) setLoading(false);
      }
    }

    load();

    return () => {
      mounted = false;
    };
  }, []);

  return { topHub, signals, loading, error };
}
```

- **Why**: Combines Candidate 1’s fallback strategy with Candidate 2’s explicit contract. Uses `connections` (preferred) or `degree`. Returns exactly 3 signals.

---

### 4) Component — `TopHubSignalCard.tsx` (clean, production-ready)
```tsx
// src/components/cards/TopHubSignalCard.tsx
import React from "react";
import { useTopHub } from "../../hooks/useTopHub";
import { Card, CardHeader, CardTitle, CardContent } from "../ui/Card";
import { Badge } from "../ui/Badge";
import { Skeleton } from "../ui/Skeleton";
import { ExternalLink } from "lucide-react";

export function TopHubSignalCard() {
  const { topHub, signals, loading, error } = useTopHub();

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-24" />
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (error || !topHub) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Top hub unavailable
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">
            Knowledge graph data not loaded.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <CardTitle className="text-sm font-semibold">Top Hub</CardTitle>
        {topHub.type && <Badge variant="secondary">{topHub.type}</Badge>}
      </CardHeader>

      <CardContent>
        <div className="mb-3">
          <h3 className="text-lg font-semibold">{topHub.label || topHub.id || "Unnamed"}</h3>
          {topHub.description && (
            <p className="text-sm text-muted-foreground">{topHub.description}</p>
          )}
          {topHub.link && (
            <a
              href={topHub.link}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
            >
              View details <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>

        <div className="space-y-2">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Signals
          </h4>
          {signals.length === 0 && (
            <p className="text-xs text-muted-foreground/70">No signals available.</p>
          )}
          {signals.map((sig) => (
            <div key={sig.id || sig.title} className="rounded border p-2 text-sm">
              <div className="flex items-start justify-between gap-2">
                <span className="font-medium">{sig.title || "Untitled
