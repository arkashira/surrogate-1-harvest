# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, telemetry-aware)

### What we ship (highest-value incremental)
- A **non-blocking Top-Hub Signal Panel** on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, top 3 signals (anomalies/recommendations), last-updated timestamp, and a “View details” link.
- **CDN-first data fetch**: uses `https://huggingface.co/datasets/{HUB_NAME}/resolve/main/signals/latest.json` (no Authorization header) to bypass HF API rate limits.
- Telemetry-aware: emits frontend timing/success metrics to `window.axentxTelemetry` (non-blocking, best-effort).
- Graceful degradation: if CDN fails or times out (>3s), panel collapses to a minimal “Signals unavailable” state without breaking the dashboard.

### Why this is highest-value (<2h)
- Reuses existing hub pattern (#knowledge-rag #hub) and CDN bypass insight.
- No backend changes, no auth, no build pipeline impact.
- Improves dashboard context for cost governance decisions immediately.
- Fits within 1–2 focused hours: one component + one telemetry helper.

---

### Implementation steps

1. Add telemetry helper (lightweight, no deps)
2. Create TopHubSignalPanel React component
3. Mount panel into dashboard layout (non-blocking placement)
4. Add env/config fallback (`HUB_NAME`, CDN URL, timeout)
5. Verify graceful failure modes and timing telemetry

---

### Code snippets

#### 1) Telemetry helper (src/lib/telemetry.js)
```js
// Minimal, non-blocking telemetry for the panel
window.axentxTelemetry = window.axentxTelemetry || {
  events: [],
  emit(name, payload = {}) {
    const entry = {
      name,
      ts: Date.now(),
      ...payload,
    };
    try {
      this.events.push(entry);
      // Best-effort flush (no network by default)
      if (this.flush) this.flush(entry);
    } catch (e) {
      // swallow — telemetry must never break UX
    }
  },
};
```

#### 2) TopHubSignalPanel component (src/components/TopHubSignalPanel.jsx)
```jsx
import React, { useEffect, useState } from 'react';

const HUB_NAME = import.meta.env.VITE_HUB_NAME || 'MOC';
const CDN_SIGNALS_URL = `https://huggingface.co/datasets/${HUB_NAME}/resolve/main/signals/latest.json`;
const FETCH_TIMEOUT_MS = 3000;

export default function TopHubSignalPanel() {
  const [state, setState] = useState({ loading: true, data: null, error: null });

  useEffect(() => {
    const start = performance.now();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      setState({ loading: false, data: null, error: 'timeout' });
      window.axentxTelemetry.emit('top_hub_fetch', { hub: HUB_NAME, status: 'timeout', durationMs: performance.now() - start });
    }, FETCH_TIMEOUT_MS);

    fetch(CDN_SIGNALS_URL, { method: 'GET', cache: 'no-store' })
      .then((res) => {
        if (timedOut) return;
        clearTimeout(timer);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        if (timedOut) return;
        const ok = Array.isArray(json?.signals) && json.signals.length > 0;
        setState({ loading: false, data: ok ? json : null, error: ok ? null : 'invalid_payload' });
        window.axentxTelemetry.emit('top_hub_fetch', {
          hub: HUB_NAME,
          status: ok ? 'ok' : 'invalid_payload',
          signalCount: ok ? json.signals.length : 0,
          durationMs: performance.now() - start,
        });
      })
      .catch((err) => {
        if (timedOut) return;
        clearTimeout(timer);
        setState({ loading: false, data: null, error: err.message || 'fetch_error' });
        window.axentxTelemetry.emit('top_hub_fetch', { hub: HUB_NAME, status: 'error', error: err.message, durationMs: performance.now() - start });
      });

    return () => clearTimeout(timer);
  }, [HUB_NAME]);

  const { loading, data, error } = state;

  if (loading) {
    return (
      <div className="p-4 border rounded bg-gray-50/50">
        <div className="flex items-center gap-2">
          <div className="w-3 h-3 rounded-full bg-blue-400 animate-pulse"></div>
          <span className="text-sm text-gray-600">Loading {HUB_NAME} signals…</span>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-4 border rounded bg-gray-50/50 opacity-60">
        <div className="text-sm text-gray-500">Top signals unavailable ({HUB_NAME})</div>
      </div>
    );
  }

  const signals = Array.isArray(data.signals) ? data.signals.slice(0, 3) : [];

  return (
    <div className="p-4 border rounded bg-white shadow-sm">
      <div className="flex items-start justify-between mb-2">
        <div>
          <h3 className="font-semibold text-gray-900">{data.title || `Top signals — ${HUB_NAME}`}</h3>
          {data.description && <p className="text-xs text-gray-500 mt-0.5">{data.description}</p>}
        </div>
        {data.updatedAt && <span className="text-xs text-gray-400">{new Date(data.updatedAt).toLocaleDateString()}</span>}
      </div>

      <ul className="space-y-2 mt-2">
        {signals.map((s, idx) => (
          <li key={idx} className="text-sm">
            <span className={`inline-block w-1.5 h-1.5 rounded-full mr-2 mt-0.5 ${s.severity === 'high' ? 'bg-red-500' : s.severity === 'medium' ? 'bg-amber-500' : 'bg-blue-500'}`}></span>
            <span className="text-gray-700">{s.title || s.message || `Signal ${idx + 1}`}</span>
            {s.actionUrl && (
              <a href={s.actionUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-600 ml-2 hover:underline">
                Details
              </a>
            )}
          </li>
        ))}
      </ul>

      {data.moreUrl && (
        <div className="mt-3 text-right">
          <a href={data.moreUrl} target="_blank" rel="noopener noreferrer" className="text-sm text-blue-600 hover:underline">
            View details →
          </a>
        </div>
      )}
    </div>
  );
}
```

#### 3) Mount into dashboard (example placement in src/pages/Dashboard.jsx)
```jsx
import TopHubSignalPanel from '../components/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <div className="p-6 space-y-6">
      {/* Existing dashboard content */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          {/* Existing cost charts/tables */}
        </div>

        {/* Non-blocking Top-Hub Signal Panel */}
        <aside className="lg:col-span-1">
          <TopHubSignalPanel />
        </aside>
      </div>
    </div>
  );
}
```

#### 4) Optional env var (VITE_HUB_NAME) in .env
```
VITE_HUB_NAME=MOC
```

---

