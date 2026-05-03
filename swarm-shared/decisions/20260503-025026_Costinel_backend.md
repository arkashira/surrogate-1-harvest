# Costinel / backend

## Final Implementation Plan  
**Top-Hub Signal Panel — CDN-first, non-blocking, <2h**

### Core Principles (resolve contradictions)
- **Correctness**: avoid HF API rate limits and auth at runtime; do not stream datasets in render.  
- **Actionability**: one working frontend-first path that can be hardened later with an optional backend cache.  
- **Non-blocking**: panel must render and be usable even if CDN, config, or telemetry fail.  
- **Telemetry**: best-effort only; no render-blocking calls, no retries, no user-visible errors.

---

### Chosen Architecture
- **Primary**: CDN-first, static file list embedded in frontend config and served from `public/cdn-lists/`.  
- **Optional future**: add `/api/signals/top-hub` as a lightweight cache/redirect (not required for MVP).  
- **No dataset streaming** in render; panel only links to resolved HF raw files.

---

### Files to modify/create

1. `src/components/TopHubSignalPanel.tsx` — new React component (CDN-first).  
2. `src/config/hubs.json` — hub metadata + CDN file list URLs.  
3. `src/hooks/useTelemetry.ts` — lightweight best-effort telemetry hook.  
4. `src/pages/Dashboard.tsx` — mount panel near cost summary.  
5. `public/cdn-lists/moc-latest.json` — sample CDN file list (committed by ops).  
6. *(optional)* `scripts/list-hub-signals.py` — Mac orchestrator to generate CDN lists.  
7. *(optional)* `backend/main.py` + `backend/config.py` — future `/api/signals/top-hub` cache.

---

### Component: TopHubSignalPanel.tsx
```tsx
// src/components/TopHubSignalPanel.tsx
import React, { useEffect, useState, useCallback } from 'react';
import { useTelemetry } from '../hooks/useTelemetry';

interface SignalItem {
  title: string;
  summary: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  cdnUrl: string;
  publishedAt: string;
}

interface HubConfig {
  name: string;
  label: string;
  description: string;
  fileListUrl: string; // CDN JSON listing for this hub/date
}

const DEFAULT_HUB = (window as any).HUB_NAME || 'MOC';

const TopHubSignalPanel: React.FC = () => {
  const [hub, setHub] = useState<HubConfig | null>(null);
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [open, setOpen] = useState(true);
  const [loading, setLoading] = useState(false);
  const emitTelemetry = useTelemetry();

  // Load hub config (non-blocking)
  useEffect(() => {
    fetch('/config/hubs.json', { cache: 'no-store' })
      .then((r) => r.json())
      .then((data: HubConfig[]) => {
        const h = data.find((x) => x.name === DEFAULT_HUB) || data[0];
        setHub(h);
      })
      .catch(() => {
        setHub({ name: DEFAULT_HUB, label: DEFAULT_HUB, description: '', fileListUrl: '' });
      });
  }, []);

  // Fetch signals from CDN file list (CDN-first)
  const fetchSignals = useCallback(async (fileListUrl: string) => {
    if (!fileListUrl) return [];
    try {
      const list = await fetch(fileListUrl, { cache: 'no-store' }).then((r) => r.json());
      // Expected: { files: [{ path, title, summary, severity, publishedAt }] }
      return (list.files || []).slice(0, 5).map((f: any) => ({
        title: f.title || f.path?.split('/').pop() || 'Signal',
        summary: f.summary || '',
        severity: ['low', 'medium', 'high', 'critical'].includes(f.severity) ? f.severity : 'medium',
        cdnUrl: `https://huggingface.co/datasets/${f.path}/resolve/main/data.json`,
        publishedAt: f.publishedAt || new Date().toISOString(),
      }));
    } catch {
      return [];
    }
  }, []);

  useEffect(() => {
    if (!hub?.fileListUrl) return;
    setLoading(true);
    fetchSignals(hub.fileListUrl)
      .then((items) => setSignals(items))
      .finally(() => setLoading(false));
  }, [hub, fetchSignals]);

  const onSignalClick = (signal: SignalItem) => {
    emitTelemetry('top_hub_signal_click', { hub: hub?.name, title: signal.title, severity: signal.severity });
    window.open(signal.cdnUrl, '_blank', 'noopener,noreferrer');
  };

  if (!open) {
    return (
      <button
        className="top-hub-collapsed"
        onClick={() => {
          setOpen(true);
          emitTelemetry('top_hub_panel_open', { hub: hub?.name });
        }}
        aria-label="Open Top Hub Signals"
      >
        🔔 {hub?.label || DEFAULT_HUB}
      </button>
    );
  }

  return (
    <aside className="top-hub-signal-panel" aria-label="Top Hub Signal Panel">
      <header>
        <strong>{hub?.label || DEFAULT_HUB}</strong>
        <span className="muted">{hub?.description || 'Top hub signals'}</span>
        <button
          className="close"
          onClick={() => {
            setOpen(false);
            emitTelemetry('top_hub_panel_close', { hub: hub?.name });
          }}
          aria-label="Close panel"
        >
          ✕
        </button>
      </header>

      <div className="signals">
        {loading && <div className="loading">Loading signals…</div>}
        {!loading && signals.length === 0 && <div className="empty">No signals</div>}
        {signals.map((s, i) => (
          <article
            key={i}
            className={`signal severity-${s.severity}`}
            onClick={() => onSignalClick(s)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') onSignalClick(s);
            }}
          >
            <div className="signal-title">{s.title}</div>
            <div className="signal-summary">{s.summary}</div>
            <div className="signal-meta">{new Date(s.publishedAt).toLocaleDateString()}</div>
          </article>
        ))}
      </div>
    </aside>
  );
};

export default TopHubSignalPanel;
```

---

### Config: hubs.json
```json
// src/config/hubs.json
[
  {
    "name": "MOC",
    "label": "MOC",
    "description": "Most-connected hub — top signals",
    "fileListUrl": "/cdn-lists/moc-latest.json"
  },
  {
    "name": "COST_OPT",
    "label": "Cost Opt",
    "description": "Cost optimization signals",
    "fileListUrl": "/cdn-lists/cost-opt-latest.json"
  }
]
```

---

### CDN file list example (committed by ops)
```json
// public/cdn-lists/moc-latest.json
{
  "files": [
    {
      "path": "moc/2026-05-03/signal-001",
      "title": "RI coverage gap in prod-east",
      "summary": "Detected 34% RI under-utilization; recommend convertible RIs for flexibility.",
      "severity": "high",
      "publishedAt": "2026-05-03T04:12:00Z"
    },
    {
      "path": "moc/2026-05-03/signal-002",
      "title": "Orphaned EBS volumes (12)",
      "summary": "Unattached gp3 volumes totaling 1.2 TB; estimated $1
