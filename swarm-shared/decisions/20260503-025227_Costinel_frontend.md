# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking, CDN-first Top-Hub Signal Panel** mounted on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, top 5 signals (title + snippet), last updated.
- Telemetry-aware: emits `panel_impression`, `signal_click`, and `panel_view_all` events; respects `window.COSTINEL_TELEMETRY=false` opt-out.
- CDN-only data fetch: uses `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/{hub}.json` to bypass HF API rate limits.
- Graceful fallback: if CDN fails, shows empty/cached stub; never blocks dashboard render; no retries.

---

### File changes (all in `/opt/axentx/Costinel`)
1. `src/components/TopHubSignalPanel.tsx` — new component  
2. `src/lib/telemetry.ts` — add lightweight telemetry helper (if missing)  
3. `src/types/hub.ts` — add types  
4. `src/pages/Dashboard.tsx` — mount panel near top of dashboard (after main KPI row)  
5. `tailwind.config.js` — ensure `aspect-ratio` plugin available (optional)

---

### 1) Types (`src/types/hub.ts`)
```ts
export interface HubSignal {
  id: string;
  title: string;
  snippet: string;
  href?: string;
  tags?: string[];
}

export interface HubData {
  hub: string;
  title: string;
  description: string;
  updatedAt: string; // ISO
  signals: HubSignal[];
}
```

---

### 2) Telemetry helper (`src/lib/telemetry.ts`)
```ts
const isTelemetryEnabled = () => window.COSTINEL_TELEMETRY !== false;

export function trackEvent(name: string, payload: Record<string, unknown> = {}) {
  if (!isTelemetryEnabled()) return;
  try {
    const body = JSON.stringify({ name, payload, ts: Date.now(), page: location.pathname });
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/_telemetry', body);
    } else {
      // Fire-and-forget; do not await or retry
      fetch('/_telemetry', { method: 'POST', body, keepalive: true }).catch(() => {});
    }
  } catch {
    // noop
  }
}
```

---

### 3) TopHubSignalPanel component (`src/components/TopHubSignalPanel.tsx`)
```tsx
import { useEffect, useState } from 'react';
import type { HubData } from '../types/hub';
import { trackEvent } from '../lib/telemetry';

const CDN_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs';
const DEFAULT_HUB = import.meta.env.VITE_HUB_NAME || 'MOC';
const MAX_SIGNALS = 5;

export default function TopHubSignalPanel({ hubName = DEFAULT_HUB }: { hubName?: string }) {
  const [data, setData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const url = `${CDN_BASE}/${encodeURIComponent(hubName)}.json?ts=${Date.now()}`;
    fetch(url, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`CDN ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData(json);
        trackEvent('panel_impression', { hub: hubName, signalCount: json.signals?.length ?? 0 });
      })
      .catch((err) => {
        setError(err.message);
        trackEvent('panel_fetch_error', { hub: hubName, error: err.message });
      })
      .finally(() => setLoading(false));
  }, [hubName]);

  const handleSignalClick = (signal: HubData['signals'][0]) => {
    trackEvent('signal_click', { hub: hubName, signalId: signal.id, signalTitle: signal.title });
  };

  // Non-blocking: render lightweight UI in all states
  return (
    <section
      aria-label={`Top hub signals — ${hubName}`}
      className="rounded-xl border border-gray-200 bg-white/60 backdrop-blur-sm p-4 shadow-sm ring-1 ring-black/5"
    >
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-gray-900">
            {loading ? 'Loading...' : data?.title ?? hubName}
          </h2>
          <p className="text-xs text-gray-500">
            {loading
              ? 'Loading signals'
              : error
              ? 'Using cached signals'
              : `Updated ${data ? new Date(data.updatedAt).toLocaleDateString() : '—'}`}
          </p>
        </div>
        <span className="inline-flex items-center rounded-full bg-cyan-50 px-2 py-0.5 text-xs font-medium text-cyan-700">
          {hubName}
        </span>
      </div>

      <div className="space-y-2" role="list">
        {loading &&
          Array.from({ length: MAX_SIGNALS }).map((_, i) => (
            <div key={i} className="h-10 animate-pulse rounded bg-gray-100" />
          ))}

        {!loading &&
          !error &&
          data &&
          data.signals.slice(0, MAX_SIGNALS).map((signal) => (
            <a
              key={signal.id}
              role="listitem"
              href={signal.href || '#'}
              onClick={() => handleSignalClick(signal)}
              className="block rounded-lg border border-transparent bg-gray-50 px-3 py-2 text-sm transition hover:border-gray-200 hover:bg-white hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
            >
              <p className="font-medium text-gray-900">{signal.title}</p>
              <p className="mt-0.5 line-clamp-2 text-xs text-gray-600">{signal.snippet}</p>
              {signal.tags && signal.tags.length > 0 && (
                <p className="mt-1 flex flex-wrap gap-1">
                  {signal.tags.slice(0, 3).map((t) => (
                    <span
                      key={t}
                      className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-gray-500"
                    >
                      {t}
                    </span>
                  ))}
                </p>
              )}
            </a>
          ))}

        {error && (
          <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
            Could not load hub signals. Displaying fallback guidance.
          </div>
        )}

        {!loading && !error && data && data.signals.length === 0 && (
          <p className="text-xs text-gray-500">No signals available for this hub.</p>
        )}
      </div>

      <div className="mt-3 text-right">
        <a
          href={`/hubs/${encodeURIComponent(hubName)}`}
          onClick={() => trackEvent('panel_view_all', { hub: hubName })}
          className="text-xs font-medium text-cyan-600 hover:underline"
        >
          View all signals →
        </a>
      </div>
    </section>
  );
}
```

---

### 4) Mount on Dashboard (`src/pages/Dashboard.tsx`)
Locate the main KPI row and insert the panel below it (or in a sidebar column depending on layout). Example placement:

```tsx
import TopHubSignalPanel from '../components/TopHubSignalPanel
