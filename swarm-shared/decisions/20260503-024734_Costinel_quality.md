# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, telemetry-aware)

### What we ship (merged + resolved)
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, **top 3 signals** (anomalies/recommendations), last updated timestamp, and a **“View details”** link.  
  *Rationale: 3 signals fits the <2h scope and avoids layout overflow; keeps UI scannable. “Refresh” button removed to reduce scope and avoid stale-state bugs; freshness is handled by `cache: 'no-store'` on each load.*
- **CDN-first data fetch**:  
  `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/hubs/{hub}/latest.json`  
  *Rationale: use `costinel-knowledge` (Candidate 1) — it matches the project name and existing knowledge-rag pattern. If `costinel-knowledge` is unavailable in production, fallback path can be added later without changing component interface.*
- **Telemetry-aware**:  
  - Respects `window.axentxTelemetry === false` (opt-out).  
  - Emits minimal, privacy-safe events via `navigator.sendBeacon` (non-blocking):  
    - `panel_impression` (after successful load)  
    - `panel_cta_click` (on “View details”)  
    - `signal_click` (if/when signals become clickable)
- **Graceful degradation**:  
  - If CDN fetch fails or returns malformed data, panel shows “Insights unavailable — will retry on next load” and does **not** block dashboard render.
  - No spinner overlay on full dashboard; panel-level loading/error states only.

---

### Files to modify/create
1. `src/components/TopHubSignalPanel.tsx` (new)
2. `src/pages/Dashboard.tsx` — import and mount panel near top.
3. `src/lib/telemetry.ts` (new) — tiny wrapper honoring opt-out and using `sendBeacon`.
4. `public/config.json` (optional) — add `"HUB_NAME": "MOC"` if not present.

---

### Code (production-ready, minimal, defensive)

#### 1) `src/lib/telemetry.ts`
```ts
// Lightweight, non-blocking telemetry with opt-out support
const ENDPOINT = '/telemetry';

export function track(event: string, payload?: Record<string, unknown>) {
  try {
    // Respect opt-out
    if ((window as any).axentxTelemetry === false) return;

    const body = JSON.stringify({
      event,
      ts: Date.now(),
      href: window.location.href,
      ...payload,
    });

    if (navigator.sendBeacon) {
      const blob = new Blob([body], { type: 'application/json' });
      navigator.sendBeacon(ENDPOINT, blob);
      return;
    }

    // Best-effort fallback
    void fetch(ENDPOINT, {
      method: 'POST',
      body,
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
    }).catch(() => {});
  } catch {
    // intentionally silent
  }
}
```

#### 2) `src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import { track } from '../lib/telemetry';

type Signal = {
  title: string;
  description: string;
  severity?: 'low' | 'medium' | 'high';
};

type HubData = {
  hub: string;
  title: string;
  description: string;
  signals: Signal[];
  updated_at: string;
};

const CDN_BASE = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/hubs';
const DEFAULT_HUB = 'MOC';
const MAX_SIGNALS = 3;

export default function TopHubSignalPanel({ hubName = DEFAULT_HUB }: { hubName?: string }) {
  const [data, setData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const url = `${CDN_BASE}/${encodeURIComponent(hubName)}/latest.json`;
    let mounted = true;

    fetch(url, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
        return res.json();
      })
      .then((json) => {
        if (!mounted) return;
        // Minimal validation
        if (!json?.hub || !Array.isArray(json.signals)) throw new Error('Invalid hub payload');
        setData(json);
        setError(false);
      })
      .catch(() => {
        if (!mounted) return;
        setError(true);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, [hubName]);

  useEffect(() => {
    if (data) {
      track('panel_impression', { hub: data.hub, signal_count: data.signals.length });
    }
  }, [data]);

  const handleCtaClick = () => {
    track('panel_cta_click', { hub: data?.hub ?? hubName });
  };

  const handleSignalClick = (idx: number) => {
    track('signal_click', { hub: data?.hub ?? hubName, signal_index: idx });
  };

  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm" aria-label="Top hub signals">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold text-gray-900">
            {loading ? 'Loading insights…' : (data?.title ?? `Hub: ${hubName}`)}
          </h2>
          <p className="mt-1 text-sm text-gray-600">
            {loading
              ? 'Fetching latest signals'
              : error
              ? 'Insights unavailable — will retry on next load'
              : data?.description ?? 'No description available'}
          </p>

          {!loading && !error && data && (
            <ul className="mt-3 space-y-2" aria-label="Top signals">
              {data.signals.slice(0, MAX_SIGNALS).map((s, i) => (
                <li key={i} className="flex items-start gap-2 text-sm">
                  <span
                    className={`mt-1.5 h-1.5 w-1.5 flex-none rounded-full ${
                      s.severity === 'high'
                        ? 'bg-red-500'
                        : s.severity === 'medium'
                        ? 'bg-amber-500'
                        : 'bg-blue-500'
                    }`}
                    aria-hidden="true"
                  />
                  <div className="min-w-0">
                    <p
                      className="font-medium text-gray-900 hover:underline cursor-pointer"
                      onClick={() => handleSignalClick(i)}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') handleSignalClick(i);
                      }}
                    >
                      {s.title}
                    </p>
                    <p className="text-gray-600">{s.description}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {error && (
            <p className="mt-3 text-xs text-gray-400">
              Using cached or fallback data not available. Panel will retry on next load.
            </p>
          )}
        </div>

        <div className="flex-shrink-0 text-right">
          <p className="text-xs text-gray-400">
            {loading
              ? ''
              : error
              ? '—'
              : `Updated ${data ? new Date(data.updated_at).toLocaleDateString() : '—'}`}
          </p>
          <a
            href={`/hubs/${encodeURIComponent
