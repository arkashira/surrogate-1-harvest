# Costinel / frontend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to the Costinel dashboard that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time. Runtime dashboard makes **zero HF API calls**. Graceful fallback when CDN data is missing. Minimal bundle impact.

---

### 1) Implementation Tasks (ordered, ~1h50m total)

| Step | Action | Time |
|------|--------|------|
| 1 | Create `scripts/build-top-hub.js` — Mac orchestration script that lists one date folder via HF API (rate-limit safe), saves `public/data/top-hub.json` | 15m |
| 2 | Create `scripts/run-top-hub-build.sh` — cron-safe wrapper with shebang, `SHELL=/bin/bash`, executable bit, safe PATH and error handling | 10m |
| 3 | Create `src/types/topHub.ts` — strict TypeScript interface | 5m |
| 4 | Create `src/lib/cdnTopHub.ts` — CDN fetch utility with SWR-like stale-while-revalidate, shape validation, and typed fallback | 10m |
| 5 | Create `src/hooks/useTopHubSignal.ts` — lazy loads baked JSON, non-blocking, handles loading/error states, avoids waterfalls | 15m |
| 6 | Create `src/components/TopHubSignalPanel.tsx` — React card component with skeleton, dark-mode styling, and graceful fallback | 30m |
| 7 | Wire into dashboard (`src/pages/Dashboard.tsx`) — lazy-mount panel in top metrics grid, non-blocking layout | 20m |
| 8 | Smoke test: build, verify CDN fetch, verify zero HF API calls in browser network tab, test offline/fallback | 20m |
| 9 | Polish: ensure no console errors, correct date formatting, and bundle size check | 15m |

**Total**: ~1h50m (includes buffer)

---

### 2) Code Snippets

#### 2.1 `public/data/top-hub.json` (committed to repo; served by CDN)
```json
{
  "hub": "MOC",
  "connections": 1274,
  "lastUpdated": "2026-05-03T00:00:00Z",
  "insight": "Most-connected hub — review before planning tasks"
}
```

#### 2.2 `src/types/topHub.ts`
```ts
export interface TopHub {
  hub: string;
  connections: number;
  lastUpdated: string; // ISO 8601
  insight: string;
}
```

#### 2.3 `src/lib/cdnTopHub.ts`
```ts
import { TopHub } from '../types/topHub';

const CDN_URL = '/data/top-hub.json';

const FALLBACK: TopHub = {
  hub: '—',
  connections: 0,
  lastUpdated: new Date().toISOString(),
  insight: 'Top-hub data unavailable',
};

function isValidTopHub(value: unknown): value is TopHub {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.hub === 'string' &&
    typeof v.connections === 'number' &&
    typeof v.lastUpdated === 'string' &&
    typeof v.insight === 'string' &&
    !Number.isNaN(Date.parse(v.lastUpdated))
  );
}

export async function fetchTopHub(): Promise<TopHub> {
  try {
    const res = await fetch(CDN_URL, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as unknown;
    if (!isValidTopHub(data)) throw new Error('Invalid shape');
    return data;
  } catch {
    return FALLBACK;
  }
}
```

#### 2.4 `src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState } from 'react';
import { fetchTopHub, type TopHub } from '../lib/cdnTopHub';

export function useTopHubSignal() {
  const [data, setData] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub()
      .then((result) => {
        if (mounted) {
          setData(result);
          setError(null);
        }
      })
      .catch((err) => {
        if (mounted) {
          setError(err instanceof Error ? err : new Error(String(err)));
          setData(null);
        }
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  return { data, loading, error };
}
```

#### 2.5 `src/components/TopHubSignalPanel.tsx`
```tsx
import { useTopHubSignal } from '../hooks/useTopHubSignal';

export function TopHubSignalPanel() {
  const { data, loading, error } = useTopHubSignal();

  if (loading) {
    return (
      <div className="animate-pulse rounded-lg bg-gray-100 p-4 dark:bg-gray-800">
        <div className="h-5 w-32 rounded bg-gray-200 dark:bg-gray-700" />
        <div className="mt-2 h-4 w-20 rounded bg-gray-200 dark:bg-gray-700" />
      </div>
    );
  }

  if (!data || error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Top-hub data unavailable
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Top-Hub Signal
          </p>
          <p className="mt-1 text-xl font-semibold text-gray-900 dark:text-gray-100">
            {data.hub}
          </p>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
            {data.connections.toLocaleString()} connections
          </p>
        </div>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-800 dark:bg-blue-900/30 dark:text-blue-300">
          Live
        </span>
      </div>
      <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">{data.insight}</p>
      <p className="mt-2 text-xs text-gray-400 dark:text-gray-500">
        Updated {new Date(data.lastUpdated).toLocaleDateString()}
      </p>
    </div>
  );
}
```

#### 2.6 `scripts/build-top-hub.js` (Mac orchestration script)
```js
#!/usr/bin/env node
/**
 * Build script: generates public/data/top-hub.json
 * Run after HF rate-limit window clears (e.g., via cron or manual).
 * Uses Hugging Face API to list a single date folder and compute top hub.
 *
 * Usage:
 *   node scripts/build-top-hub.js
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const HF_API_BASE = 'https://huggingface.co/api';
const REPO = 'your
