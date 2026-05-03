# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**.

**Why this ships in <2h**:
- No backend changes; static asset strategy.
- Reuses existing patterns (knowledge-rag top-hub, CDN bypass).
- Pure frontend + build-time generation.

---

### 1. File layout (additions only)

```
/opt/axentx/Costinel/
├── public/
│   └── signals/
│       └── top-hub.json          # baked at build/CI; served via CDN
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.tsx
│   ├── hooks/
│   │   └── useTopHubSignal.ts
│   └── types/
│       └── signal.d.ts
└── package.json                  # optional: add build script to bake JSON
```

---

### 2. Type definitions

`src/types/signal.d.ts`
```ts
export interface TopHubSignal {
  hubId: string;
  label: string;
  description: string;
  rank: number;
  connections: number;
  lastUpdated: string; // ISO
  signals: Array<{
    id: string;
    title: string;
    summary: string;
    severity: 'low' | 'medium' | 'high' | 'critical';
    action?: string;
  }>;
  cdnPath: string;
}
```

---

### 3. CDN-first hook (zero runtime HF API)

`src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState } from 'react';
import type { TopHubSignal } from '../types/signal';

const CDN_TOP_HUB_URL = '/signals/top-hub.json';
const FALLBACK: TopHubSignal = {
  hubId: 'moc',
  label: 'MOC',
  description: 'Most-connected operational hub (fallback).',
  rank: 1,
  connections: 0,
  lastUpdated: new Date().toISOString(),
  signals: [],
  cdnPath: CDN_TOP_HUB_URL,
};

export function useTopHubSignal(options?: { refreshIntervalMs?: number }) {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchSignal = async () => {
    try {
      // CDN fetch — no Authorization header; bypasses HF API rate limits
      const res = await fetch(CDN_TOP_HUB_URL, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
      const data = (await res.json()) as TopHubSignal;
      setSignal(data);
      setError(null);
    } catch (err) {
      setError(err as Error);
      setSignal(FALLBACK);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSignal();
    if (options?.refreshIntervalMs && options.refreshIntervalMs > 0) {
      const id = setInterval(fetchSignal, options.refreshIntervalMs);
      return () => clearInterval(id);
    }
  }, [options?.refreshIntervalMs]);

  return { signal, loading, error, refetch: fetchSignal };
}
```

---

### 4. Top-Hub Signal Panel component

`src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import type { TopHubSignal } from '../types/signal';

const severityColors = {
  low: 'bg-gray-100 text-gray-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-orange-100 text-orange-800',
  critical: 'bg-red-100 text-red-800',
} as const;

export function TopHubSignalPanel() {
  const { signal, loading, error } = useTopHubSignal({ refreshIntervalMs: 300_000 });

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-2 h-4 w-24 animate-pulse rounded bg-gray-100" />
      </div>
    );
  }

  if (error || !signal) {
    return (
      <div className="rounded-lg border border-red-100 bg-red-50 p-4 text-sm text-red-700">
        Unable to load top-hub signal. Using fallback.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm">
      {/* Header */}
      <div className="flex items-start justify-between border-b border-gray-100 p-4">
        <div>
          <h3 className="text-base font-semibold text-gray-900">{signal.label}</h3>
          <p className="text-sm text-gray-500">{signal.description}</p>
        </div>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700">
          Rank {signal.rank}
        </span>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-4 border-b border-gray-100 px-4 py-3 text-sm">
        <div>
          <span className="text-gray-500">Connections</span>
          <div className="mt-1 text-lg font-semibold text-gray-900">{signal.connections}</div>
        </div>
        <div className="text-right">
          <span className="text-gray-500">Last updated</span>
          <div className="mt-1 text-xs text-gray-500">
            {new Date(signal.lastUpdated).toLocaleString()}
          </div>
        </div>
      </div>

      {/* Signals */}
      <div className="max-h-60 overflow-y-auto p-4">
        {signal.signals.length === 0 ? (
          <p className="text-sm text-gray-500">No active signals for this hub.</p>
        ) : (
          <ul className="space-y-2" role="list">
            {signal.signals.map((s) => (
              <li key={s.id} className="rounded border border-gray-100 p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-gray-900">{s.title}</p>
                    <p className="mt-0.5 text-xs text-gray-500">{s.summary}</p>
                    {s.action && (
                      <p className="mt-1 text-xs text-blue-600 hover:underline cursor-pointer">
                        {s.action}
                      </p>
                    )}
                  </div>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${severityColors[s.severity]}`}>
                    {s.severity}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Footer */}
      <div className="border-t border-gray-100 px-4 py-2 text-xs text-gray-400">
        Source: CDN — {signal.cdnPath}
      </div>
    </div>
  );
}
```

---

### 5. Static JSON baked at build/CI (example)

`public/signals/top-hub.json`
```json
{
  "hubId":
