# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, telemetry-aware)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, top 5 signals (anomalies/recommendations), last-updated timestamp, and a “View full hub” link.
- **CDN-first data strategy**: pre-listed file paths embedded at build time; runtime fetches use `https://huggingface.co/datasets/.../resolve/main/...` (no Authorization header) to bypass HF API rate limits.
- **Telemetry-aware**: respects `data-telemetry-optout` and emits minimal, privacy-safe frontend events (`panel_impression`, `signal_click`) only when telemetry enabled.
- **Zero backend changes** — pure frontend addition.

### Why this is highest-value (<2h)
- Leverages existing knowledge-rag/graph patterns (MOC hub) and CDN-bypass pattern to deliver contextual insights without backend work or rate-limit risk.
- Small, scoped UI surface that can ship immediately and be iterated.

---

### File changes

#### 1) Add panel component
`src/components/TopHubSignalPanel.jsx`

```jsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

const HUB_NAME = import.meta.env.VITE_HUB_NAME || 'MOC';
const CDN_DATA_URL = import.meta.env.VITE_HUB_DATA_URL || 
  `https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/hubs/${HUB_NAME}/signals.json`;

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const telemetryEnabled = document.documentElement.dataset.telemetryOptout !== 'true';

  function emitEvent(name, payload = {}) {
    if (!telemetryEnabled) return;
    try {
      window.dispatchEvent(new CustomEvent('frontend_telemetry', {
        detail: { name, payload, source: 'TopHubSignalPanel', ts: Date.now() }
      }));
    } catch (e) {
      // noop
    }
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    fetch(CDN_DATA_URL, { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load hub data: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        setHub(data);
        emitEvent('panel_impression', { hub: HUB_NAME, signalCount: data?.signals?.length ?? 0 });
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message);
        // soft fail — panel renders empty rather than breaking dashboard
        console.warn('[TopHubSignalPanel]', err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [CDN_DATA_URL]);

  if (loading) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        <div className="skeleton-title"></div>
        <div className="skeleton-list"></div>
      </div>
    );
  }

  if (error || !hub) {
    // Non-blocking: render minimal placeholder so dashboard unaffected
    return null;
  }

  return (
    <aside className="top-hub-panel" aria-label={`Top hub: ${hub.title}`}>
      <header className="top-hub-header">
        <div>
          <h3 className="top-hub-title">{hub.title}</h3>
          <p className="top-hub-desc">{hub.description}</p>
        </div>
        {hub.updatedAt && (
          <time className="top-hub-updated" dateTime={hub.updatedAt}>
            Updated {new Date(hub.updatedAt).toLocaleDateString()}
          </time>
        )}
      </header>

      <ul className="top-hub-signals" aria-live="polite">
        {(hub.signals || []).slice(0, 5).map((s, idx) => (
          <li key={s.id || idx} className="top-hub-signal">
            <button
              className="top-hub-signal-btn"
              onClick={() => {
                emitEvent('signal_click', { hub: HUB_NAME, signalId: s.id, title: s.title });
                // If signal has a link, open in new tab; otherwise noop (panel is non-blocking)
                if (s.url) window.open(s.url, '_blank', 'noopener,noreferrer');
              }}
              aria-label={s.title}
            >
              <span className="top-hub-signal-badge">{s.category || 'Signal'}</span>
              <span className="top-hub-signal-text">{s.title}</span>
              {s.url && <span className="top-hub-signal-icon" aria-hidden="true">→</span>}
            </button>
            {s.description && <p className="top-hub-signal-desc">{s.description}</p>}
          </li>
        ))}
      </ul>

      {hub.viewUrl && (
        <footer className="top-hub-footer">
          <a href={hub.viewUrl} target="_blank" rel="noopener noreferrer" className="top-hub-link">
            View full hub →
          </a>
        </footer>
      )}
    </aside>
  );
}
```

#### 2) Panel styles
`src/components/TopHubSignalPanel.css`

```css
.top-hub-panel {
  background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 12px;
  padding: 16px;
  min-width: 260px;
  max-width: 360px;
}

.top-hub-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
}

.top-hub-title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: #e6eef6;
}

.top-hub-desc {
  font-size: 12px;
  color: #9aa7b8;
  margin: 0;
}

.top-hub-updated {
  font-size: 11px;
  color: #5a6a7a;
}

.top-hub-signals {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.top-hub-signal-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  padding: 6px 0;
  cursor: pointer;
  color: inherit;
  font: inherit;
}

.top-hub-signal-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(91,192,235,0.12);
  color: #5bc0eb;
  flex-shrink: 0;
}

.top-hub-signal-text {
  font-size: 13px;
  color: #cfe0ea;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1 1 auto;
}

.top-hub-signal-icon {
  font-size
