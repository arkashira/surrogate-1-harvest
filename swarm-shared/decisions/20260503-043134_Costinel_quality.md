# Costinel / quality

```
├─ src/
│  ├─ components/
│  │  └─ TopHubSignalPanel.tsx      (new)
│  ├─ hooks/
│  │  └─ useTopHubSignal.ts         (new)
│  ├─ lib/
│  │  └─ cdn.ts                     (new)
│  └─ pages/
│     └─ Dashboard.tsx              (modify)
└─ scripts/
   └─ fetch-top-hub-manifest.sh     (new)
```

---

### 2) Implementation plan (steps)

1. **Create CDN fetcher** (`src/lib/cdn.ts`)  
   - Fetch `hub-manifest.json` from `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hub-manifest.json` (no auth, CDN bypass).  
   - Optional: accept local override via `PUBLIC_HUB_MANIFEST_URL` for dev.

2. **Create hook** (`src/hooks/useTopHubSignal.ts`)  
   - Load manifest via CDN fetcher.  
   - Pick top hub by `connections` (desc).  
   - Return `{ hub, relatedDocs, lastUpdated }`.  
   - Handle 429/404 gracefully (cached stale-while-revalidate).

3. **Create component** (`src/components/TopHubSignalPanel.tsx`)  
   - Display hub name, short description, top 3 related docs (title + snippet + link).  
   - Show last-updated timestamp.  
   - Empty/loading/error states.  
   - Style: minimal card consistent with existing dashboard.

4. **Wire into Dashboard** (`src/pages/Dashboard.tsx`)  
   - Import and place `<TopHubSignalPanel />` in the signals/insights section.

5. **Add build-time fetch script** (`scripts/fetch-top-hub-manifest.sh`)  
   - Optional: pre-fetch manifest at build time and copy into `public/hub-manifest.json` for zero-runtime dependency.  
   - Uses `curl` with retries and 360s backoff on 429.

6. **Tests & lint**  
   - Quick smoke test in dev; ensure no secrets in repo.

---

### 3) Code snippets

#### `src/lib/cdn.ts`
```ts
const DEFAULT_MANIFEST_URL =
  'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hub-manifest.json';

export interface RelatedDoc {
  title: string;
  snippet: string;
  url: string;
  score?: number;
}

export interface HubEntry {
  hub: string;
  description: string;
  connections: number;
  related: RelatedDoc[];
  updatedAt: string; // ISO
}

export interface HubManifest {
  generatedAt: string;
  hubs: HubEntry[];
}

export async function fetchHubManifest(
  url = DEFAULT_MANIFEST_URL,
  options: RequestInit = {}
): Promise<HubManifest> {
  const res = await fetch(url, {
    cache: 'no-store',
    ...options,
  });

  if (!res.ok) {
    const err = new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);
    (err as any).status = res.status;
    throw err;
  }

  return res.json() as Promise<HubManifest>;
}
```

#### `src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import { fetchHubManifest, HubEntry, HubManifest } from '../lib/cdn';

const STALE_MS = 5 * 60 * 1000; // 5m stale tolerance for UI

export function useTopHubSignal(manifestUrl?: string) {
  const [data, setData] = useState<{ hub: HubEntry | null; lastUpdated: string | null }>({
    hub: null,
    lastUpdated: null,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const manifest: HubManifest = await fetchHubManifest(manifestUrl);
      const top = manifest.hubs
        .slice()
        .sort((a, b) => (b.connections ?? 0) - (a.connections ?? 0))[0] || null;

      setData({
        hub: top,
        lastUpdated: manifest.generatedAt || new Date().toISOString(),
      });
    } catch (err: any) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [manifestUrl]);

  useEffect(() => {
    load();
    // Optional: refresh interval (30m)
    const id = setInterval(load, 30 * 60 * 1000);
    return () => clearInterval(id);
  }, [load]);

  return {
    hub: data.hub,
    lastUpdated: data.lastUpdated,
    loading,
    error,
    refetch: load,
  };
}
```

#### `src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import { RelatedDoc } from '../lib/cdn';

function DocItem({ doc }: { doc: RelatedDoc }) {
  return (
    <a
      href={doc.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block p-2 rounded border border-gray-200 hover:border-blue-300 hover:bg-blue-50 transition-colors"
    >
      <div className="font-medium text-gray-900">{doc.title}</div>
      <div className="text-sm text-gray-600 mt-1 line-clamp-2">{doc.snippet}</div>
    </a>
  );
}

export default function TopHubSignalPanel() {
  const { hub, lastUpdated, loading, error, refetch } = useTopHubSignal();

  if (loading) {
    return (
      <div className="p-4 border border-gray-200 rounded bg-gray-50">
        <div className="text-sm text-gray-500">Loading top hub signal...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 border border-red-200 rounded bg-red-50">
        <div className="text-sm text-red-700">
          Could not load top hub signal.{' '}
          <button onClick={() => refetch()} className="underline text-blue-700">
            Retry
          </button>
        </div>
        <div className="text-xs text-red-500 mt-1">{(error as Error).message}</div>
      </div>
    );
  }

  if (!hub) {
    return (
      <div className="p-4 border border-gray-200 rounded bg-gray-50">
        <div className="text-sm text-gray-500">No hub signal available.</div>
      </div>
    );
  }

  return (
    <div className="p-4 border border-gray-200 rounded bg-white shadow-sm">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-gray-900">Top Hub Signal</h3>
          <div className="text-sm text-gray-500">Most-connected hub</div>
        </div>
        {lastUpdated && (
          <div className="text-xs text-gray-400" title={lastUpdated}>
            Updated {new Date(lastUpdated).toLocaleString()}
          </div>
        )}
      </div>

      <div className="mb-3">
        <div className="text-lg font-bold text-blue-700">{hub.hub}</div>
        <div className="text-sm text-gray-600">{hub.description}</div>
        <div className="text-xs text-gray-400 mt-1">
          Connections: {hub.connections}
        </
