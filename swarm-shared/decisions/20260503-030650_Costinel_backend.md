# Costinel / backend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### Scope (highest-value incremental improvement)
Add a lightweight, non-blocking **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default: `MOC`) with contextual insights from the knowledge-rag graph. Uses CDN-first pattern to avoid HF API rate limits and keeps backend changes minimal.

### Architecture (no blocking calls)
- **Frontend**: React component in `/src/components/TopHubSignalPanel.tsx` + hook `useTopHubInsights.ts` (fetches CDN JSON).
- **Backend**: Optional tiny endpoint `/api/hub-insights` (GET) that proxies CDN JSON (or returns local file) for SSR/auth needs; if not needed, frontend-only is fine.
- **Data flow**:
  1. Mac runs `scripts/fetch-hub-list.js` (once per day or per deploy) → `public/data/hub-list.json` (list of hubs + file paths).
  2. Component fetches `https://huggingface.co/datasets/{repo}/resolve/main/data/hubs/{hubName}.json` (CDN, no auth) or falls back to `/data/hubs/{hubName}.json`.
  3. Renders title, short description, top 3 signals, and quick actions.

### Files to create/modify
- `src/components/TopHubSignalPanel.tsx`
- `src/hooks/useTopHubInsights.ts`
- `public/data/hubs/MOC.json` (sample)
- `scripts/fetch-hub-list.js` (optional automation)
- `src/App.tsx` or dashboard layout (mount panel)
- `vite-env.d.ts` (env types)

### Environment
- `VITE_HUB_NAME=MOC` (default)
- `VITE_HUB_DATASET_REPO=axentx/costinel-hubs` (or keep local)

---

## Code Snippets

### 1) Hook: `src/hooks/useTopHubInsights.ts`
```ts
// src/hooks/useTopHubInsights.ts
import { useEffect, useState } from 'react';

export interface HubSignal {
  id: string;
  title: string;
  description: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  action?: { label: string; href: string };
}

export interface HubInsights {
  hubName: string;
  title: string;
  shortDescription: string;
  updatedAt: string;
  signals: HubSignal[];
}

const DEFAULT_HUB = import.meta.env.VITE_HUB_NAME || 'MOC';
const DATASET_REPO = import.meta.env.VITE_HUB_DATASET_REPO || '';

export function useTopHubInsights(hubName: string = DEFAULT_HUB) {
  const [insights, setInsights] = useState<HubInsights | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    async function fetchInsights() {
      try {
        // CDN-first: try public HF CDN if dataset repo is configured
        if (DATASET_REPO) {
          const cdnUrl = `https://huggingface.co/datasets/${DATASET_REPO}/resolve/main/data/hubs/${hubName}.json`;
          const res = await fetch(cdnUrl, { cache: 'no-store' });
          if (res.ok) {
            const json = await res.json();
            if (mounted) {
              setInsights(json);
              setLoading(false);
              return;
            }
          }
        }

        // Fallback to local/public file
        const localRes = await fetch(`/data/hubs/${hubName}.json`, { cache: 'no-store' });
        if (!localRes.ok) throw new Error('Local hub file not found');
        const json = await localRes.json();
        if (mounted) {
          setInsights(json);
        }
      } catch (err: any) {
        if (mounted) setError(err.message || 'Failed to load hub insights');
      } finally {
        if (mounted) setLoading(false);
      }
    }

    fetchInsights();

    return () => {
      mounted = false;
    };
  }, [hubName]);

  return { insights, loading, error };
}
```

### 2) Component: `src/components/TopHubSignalPanel.tsx`
```tsx
// src/components/TopHubSignalPanel.tsx
import React from 'react';
import { useTopHubInsights } from '../hooks/useTopHubInsights';

const severityColors = {
  low: 'bg-gray-100 text-gray-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-orange-100 text-orange-800',
  critical: 'bg-red-100 text-red-800',
};

export default function TopHubSignalPanel({ hubName }: { hubName?: string }) {
  const { insights, loading, error } = useTopHubInsights(hubName);

  if (loading) {
    return (
      <div className="p-3 border-b bg-gray-50/50">
        <div className="h-4 w-32 bg-gray-200 rounded animate-pulse" />
      </div>
    );
  }

  if (error || !insights) {
    // Fail silently (non-blocking) — render minimal placeholder
    return null;
  }

  return (
    <div className="p-3 border-b bg-gradient-to-r from-blue-50 to-indigo-50">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-gray-900 truncate">{insights.title}</h3>
            <span className="text-xs text-gray-500">{insights.hubName}</span>
          </div>
          <p className="text-xs text-gray-600 mt-0.5">{insights.shortDescription}</p>

          {insights.signals && insights.signals.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {insights.signals.slice(0, 3).map((s) => (
                <div
                  key={s.id}
                  className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs border"
                >
                  <span className={`w-1.5 h-1.5 rounded-full ${severityColors[s.severity]}`} />
                  <span className="font-medium">{s.title}</span>
                  {s.action && (
                    <a
                      href={s.action.href}
                      className="underline text-blue-600 hover:text-blue-800 ml-1"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {s.action.label}
                    </a>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex-shrink-0">
          <a
            href={`/hubs/${insights.hubName}`}
            className="text-xs text-blue-600 hover:text-blue-800 underline"
          >
            View hub
          </a>
        </div>
      </div>

      <div className="mt-2 text-xs text-gray-400">
        Updated {new Date(insights.updatedAt).toLocaleString()}
      </div>
    </div>
  );
}
```

### 3) Mount in dashboard layout (example)
```tsx
// In your dashboard page or App.tsx
import TopHubSignalPanel from './components/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <div>
      <TopHubSignalPanel />
      {/* rest of dashboard */}
    </div>
  );
}
```

### 4) Sample hub file: `public/data/hubs/MOC.json`
```json
{
  "hubName": "MOC",
  "title": "MOC — Most Connected Hub",
  "shortDescription": "Central hub
