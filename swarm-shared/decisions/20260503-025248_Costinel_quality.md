# Costinel / quality

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, telemetry-aware)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard sidebar/top area.
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, top 5 signals (anomalies/recommendations), quick link to hub detail.
- **CDN-first data fetch**: uses `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/{hubName}.json` to bypass HF API rate limits.
- **Telemetry-aware**: respects `data-telemetry-optout` on root element; if opted out, panel shows generic guidance instead of live signals.
- **Graceful fallback**: if CDN fails or hub missing, shows cached minimal guidance (no broken UI).

### Files to modify/create
1. `src/components/TopHubSignalPanel.tsx` — new component.
2. `src/services/hubService.ts` — CDN fetcher + cache.
3. `src/config/hubs.ts` — hub metadata (MOC default).
4. `src/App.tsx` (or dashboard layout) — mount panel.
5. `vite-env.d.ts` — add env types (if not present).

### Step-by-step (≤2h)
1. Add env constant for hub name (5m).
2. Create hub service with CDN fetch + 5-minute in-memory cache (15m).
3. Create TopHubSignalPanel component with telemetry check and graceful fallback (30m).
4. Wire into dashboard layout (10m).
5. Add minimal styling (15m).
6. Verify CDN path and test with mocked failure (15m).

---

## Code snippets

### 1) Env + hub config
```ts
// src/config/hubs.ts
export const HUB_NAME = import.meta.env.VITE_HUB_NAME || 'MOC';
export const HUB_CDN_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs';

export interface HubSignal {
  id: string;
  title: string;
  description: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  recommendation?: string;
  href?: string;
}

export interface HubData {
  name: string;
  title: string;
  description: string;
  signals: HubSignal[];
  updatedAt: string; // ISO
}
```

### 2) Hub service (CDN-first, cache, no API rate limit)
```ts
// src/services/hubService.ts
import { HUB_CDN_BASE } from '../config/hubs';

type HubData = import('../config/hubs').HubData;

const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes
const cache = new Map<string, { data: HubData | null; ts: number }>();

export async function fetchHubData(hubName: string): Promise<HubData | null> {
  const now = Date.now();
  const cached = cache.get(hubName);
  if (cached && now - cached.ts < CACHE_TTL_MS) {
    return cached.data;
  }

  const url = `${HUB_CDN_BASE}/${encodeURIComponent(hubName)}.json`;
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const data = (await res.json()) as HubData;
    cache.set(hubName, { data, ts: now });
    return data;
  } catch (err) {
    console.warn('[HubService] CDN fetch failed, using fallback', err);
    cache.set(hubName, { data: null, ts: now });
    return null;
  }
}
```

### 3) TopHubSignalPanel component
```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import { fetchHubData } from '../services/hubService';
import { HUB_NAME } from '../config/hubs';
import type { HubData, HubSignal } from '../config/hubs';

function isTelemetryOptOut(): boolean {
  const root = document.documentElement;
  return root.getAttribute('data-telemetry-optout') === 'true';
}

const GENERIC_SIGNALS: HubSignal[] = [
  {
    id: 'guidance',
    title: 'Review cost anomalies daily',
    description: 'Check the Costinel dashboard for real-time anomalies and recommendations.',
    severity: 'medium',
  },
  {
    id: 'ri-coverage',
    title: 'Validate RI/SP coverage',
    description: 'Ensure reserved instance coverage aligns with predictable workloads.',
    severity: 'low',
  },
];

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const telemetryOptOut = isTelemetryOptOut();

  useEffect(() => {
    if (telemetryOptOut) {
      setHub({
        name: HUB_NAME,
        title: 'Costinel Guidance',
        description: 'Live signals disabled per telemetry preference.',
        signals: GENERIC_SIGNALS,
        updatedAt: new Date().toISOString(),
      });
      setLoading(false);
      return;
    }

    let mounted = true;
    setLoading(true);
    fetchHubData(HUB_NAME)
      .then((data) => {
        if (!mounted) return;
        if (data) {
          setHub(data);
        } else {
          // fallback minimal
          setHub({
            name: HUB_NAME,
            title: HUB_NAME,
            description: 'Hub insights unavailable — showing guidance.',
            signals: GENERIC_SIGNALS,
            updatedAt: new Date().toISOString(),
          });
        }
      })
      .catch(() => {
        if (!mounted) return;
        setHub({
          name: HUB_NAME,
          title: HUB_NAME,
          description: 'Unable to load hub data.',
          signals: GENERIC_SIGNALS,
          updatedAt: new Date().toISOString(),
        });
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, [telemetryOptOut]);

  const severityColor = (s: HubSignal['severity']) => {
    switch (s) {
      case 'critical': return 'text-red-600 bg-red-50 border-red-200';
      case 'high': return 'text-orange-600 bg-orange-50 border-orange-200';
      case 'medium': return 'text-amber-600 bg-amber-50 border-amber-200';
      default: return 'text-gray-600 bg-gray-50 border-gray-200';
    }
  };

  if (loading) {
    return (
      <div className="p-3 border rounded-lg bg-gray-50 animate-pulse">
        <div className="h-4 w-32 bg-gray-200 rounded mb-2"></div>
        <div className="h-3 w-full bg-gray-200 rounded mb-1"></div>
        <div className="h-3 w-5/6 bg-gray-200 rounded"></div>
      </div>
    );
  }

  if (!hub) return null;

  return (
    <div className="p-3 border rounded-lg bg-white shadow-sm">
      <div className="flex items-start justify-between mb-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{hub.title}</h3>
          <p className="text-xs text-gray-500">{hub.description}</p>
        </div>
        <span className="text-xs text-gray-400">
          Updated {new Date(hub.updatedAt).toLocaleDateString()}
        </span>
      </div>

      <div className="space-y-2 mt-2">
        {hub.signals.slice(0, 5).map((s) => (
          <a
            key={s.id}
            href
