# Costinel / backend

## Final Implementation — CDN-First Top-Hub Signal Panel (<2h)

**Scope**  
Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel that surfaces the highest-centrality hub (e.g., “MOC”) using CDN-first data with zero runtime HF API calls. The panel is strictly frontend + build-time asset, robust to CDN failures, and degrades gracefully.

---

### Why this ships in <2h
- No backend or training/inference changes.
- CDN fetch bypasses HF API auth/rate limits.
- Build-time baked JSON + local fallback ensures availability even when CDN is unreachable.
- Reuses existing patterns (top-hub insight, CDN bypass, graceful degradation).

---

### File changes

1) Data asset (committed + mirrored to CDN)  
`public/data/top-hub/moc.json`

```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "score": 0.94,
  "connections": 127,
  "lastUpdated": "2026-05-03T04:00:00.000Z",
  "summary": "Highest centrality hub across cost governance signals. Primary coordination point for anomaly triage and policy propagation.",
  "recommendations": [
    "Validate RI coverage for MOC-linked accounts",
    "Audit cross-region egress from MOC services",
    "Apply budget guardrails to MOC owner team"
  ],
  "cdnPath": "https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub/moc.json"
}
```

2) React component  
`src/components/TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState, useCallback } from 'react';
import { AlertCircle, TrendingUp, Network } from 'lucide-react';

type TopHubData = {
  hub: string;
  label: string;
  score: number;
  connections: number;
  lastUpdated: string;
  summary: string;
  recommendations: string[];
  cdnPath?: string;
};

const CDN_PATH = 'https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub/moc.json';
const LOCAL_PATH = '/data/top-hub/moc.json';

// Keep minimal static fallback only for extreme failure cases (do not bundle large JSON here).
const STATIC_FALLBACK: TopHubData | null = null;

const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchJSON = useCallback(async (url: string): Promise<TopHubData> => {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Fetch failed: ${res.status}`);
    return res.json();
  }, []);

  useEffect(() => {
    let mounted = true;

    async function load() {
      try {
        // 1) CDN first (bypasses HF API limits)
        const cdnData = await fetchJSON(CDN_PATH);
        if (mounted) {
          setData(cdnData);
          setError(null);
          return;
        }
      } catch {
        // noop — proceed to fallback
      }

      try {
        // 2) Local same-origin fallback (bundled asset)
        const localData = await fetchJSON(LOCAL_PATH);
        if (mounted) {
          setData(localData);
          setError(null);
          return;
        }
      } catch {
        // noop — proceed to final fallback
      }

      try {
        // 3) Static import fallback (dev-only, if bundler supports)
        if (STATIC_FALLBACK && mounted) {
          setData(STATIC_FALLBACK);
          setError(null);
          return;
        }
      } catch {
        // noop
      }

      if (mounted) {
        setError('Unable to load top-hub signal. Showing degraded view.');
        setData(null);
      }
    } finally {
      if (mounted) setLoading(false);
    }

    load();

    return () => {
      mounted = false;
    };
  }, [fetchJSON]);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card/50 p-4 animate-pulse">
        <div className="h-5 w-32 bg-muted rounded mb-2" />
        <div className="h-4 w-full bg-muted rounded" />
      </div>
    );
  }

  const scorePct = Math.round((data?.score ?? 0) * 100);

  return (
    <div className="rounded-lg border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/60 p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex items-center gap-2">
          <Network className="h-5 w-5 text-primary" />
          <div>
            <h3 className="font-semibold text-sm leading-none">Top-Hub Signal</h3>
            <p className="text-xs text-muted-foreground">{data?.label ?? '—'}</p>
          </div>
        </div>
        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          <TrendingUp className="h-3 w-3" />
          <span>{scorePct}% centrality</span>
        </div>
      </div>

      {error && (
        <div className="mb-3 flex items-center gap-2 text-xs text-amber-600 bg-amber-50 dark:bg-amber-950/30 rounded-md px-3 py-2">
          <AlertCircle className="h-3 w-3 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {data && (
        <>
          <p className="text-sm text-muted-foreground mb-3">{data.summary}</p>

          <div className="mb-3">
            <div className="flex justify-between text-xs text-muted-foreground mb-1">
              <span>Connections</span>
              <span className="font-medium text-foreground">{data.connections}</span>
            </div>
            <div className="h-2 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full bg-primary transition-all duration-500 ease-out"
                style={{ width: `${Math.min(scorePct, 100)}%` }}
              />
            </div>
          </div>

          <div className="text-xs">
            <div className="font-medium mb-2">Recommended actions</div>
            <ul className="space-y-1 text-muted-foreground">
              {data.recommendations.map((rec, idx) => (
                <li key={idx} className="flex gap-2">
                  <span className="text-primary">•</span>
                  <span>{rec}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="mt-3 pt-3 border-t border-border/40 text-xs text-muted-foreground">
            Updated {new Date(data.lastUpdated).toLocaleDateString(undefined, {
              month: 'short',
              day: 'numeric',
              hour: '2-digit',
              minute: '2-digit',
            })}
          </div>
        </>
      )}
    </div>
  );
};

export default TopHubSignalPanel;
```

3) Add to dashboard layout  
Example placement in `src/pages/Dashboard.tsx` (or equivalent):

```tsx
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

// Inside your dashboard grid:
<div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
  <TopHubSignalPanel />
  {/* other panels/cards */}
</div>
```

---

### Key decisions (resolved contradictions)
- CDN-first, zero HF API at runtime: avoids auth
