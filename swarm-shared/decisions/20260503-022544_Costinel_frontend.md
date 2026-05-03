# Costinel / frontend

**Final Implementation — Top-Hub Signal Panel (Costinel Dashboard)**

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data, zero backend changes, ships in <2h. Aligns with “Sense + Signal — don’t Execute”.

---

### Architecture Decisions (merged)

- **Data source**:  
  - Primary: CDN asset at `/data/knowledge-graph/moc-hub.json` (or repo raw) so frontend bypasses HF API rate limits.  
  - Fallback: local `public/data/knowledge-graph/moc-hub.json` and config-driven path via `src/config/topHub.json`.
- **Component**: `TopHubSignalPanel` — card on dashboard showing hub title, short description, and top 3 proposals with action chips.
- **UX**: If data unavailable → graceful empty state with tooltip “Enable knowledge-rag to populate signals”.
- **Performance & resilience**:  
  - `fetch` with `cache: 'force-cache'` + simple stale-while-revalidate via `useEffect`.  
  - CDN-first with local fallback; timeouts and error boundaries prevent dashboard breakage.
- **Routing**: Panel is embedded directly in `Dashboard.jsx` (no new route) to minimize surface area.

---

### File Changes (concrete, repo-relative)

#### 1) Config (new)

`src/config/topHub.json`
```json
{
  "hubId": "MOC",
  "label": "Mission Operations Center",
  "cdnUrl": "https://cdn.example.com/data/knowledge-graph/moc-hub.json",
  "localUrl": "/data/knowledge-graph/moc-hub.json",
  "timeoutMs": 4000
}
```

#### 2) Local fallback data (new)

`public/data/knowledge-graph/moc-hub.json`
```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "description": "Central hub for cross-cloud cost governance, anomaly detection, and proposal lifecycle management.",
  "proposals": [
    {
      "id": "moc-001",
      "title": "Enforce RI coverage for top 5 spend services",
      "impact": "high",
      "effort": "medium",
      "signal": "Detected 38% underutilized on-demand spend in us-east-1.",
      "cta": "Review RI plan"
    },
    {
      "id": "moc-002",
      "title": "Schedule dev/staging off-hours shutdown",
      "impact": "medium",
      "effort": "low",
      "signal": "Non-prod accounts run 24/7; estimated 22% nightly waste.",
      "cta": "Configure schedules"
    },
    {
      "id": "moc-003",
      "title": "Tag enforcement policy for untagged resources",
      "impact": "medium",
      "effort": "low",
      "signal": "14% of resources missing cost-center tag; limits chargeback accuracy.",
      "cta": "Apply policy"
    }
  ],
  "updatedAt": "2026-05-03T02:30:00Z"
}
```

#### 3) Panel component (new)

`src/components/TopHubSignalPanel.jsx`
```jsx
import React, { useEffect, useState } from 'react';
import config from '../config/topHub.json';
import './TopHubSignalPanel.scss';

const impactColor = {
  high: 'var(--signal-high, #16a34a)',
  medium: 'var(--signal-medium, #f59e0b)',
  low: 'var(--signal-low, #64748b)',
};

function tryGetUrl() {
  // Prefer CDN when available in browser context; fallback to local.
  if (typeof window !== 'undefined' && window.location.protocol === 'https:') {
    return config.cdnUrl || config.localUrl;
  }
  return config.localUrl;
}

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), config.timeoutMs || 4000);

    async function load() {
      try {
        const primaryUrl = tryGetUrl();
        const res = await fetch(primaryUrl, {
          cache: 'force-cache',
          signal: controller.signal,
        });

        if (!res.ok) throw new Error(`Failed to load hub data: ${res.status}`);
        const data = await res.json();
        if (mounted) {
          setHub(data);
          setError(null);
        }
      } catch (err) {
        // If CDN failed and we weren't already using local, try local fallback.
        if (mounted && tryGetUrl() !== config.localUrl) {
          try {
            const localRes = await fetch(config.localUrl, {
              cache: 'force-cache',
              signal: controller.signal,
            });
            if (!localRes.ok) throw new Error(`Local fallback failed: ${localRes.status}`);
            const data = await localRes.json();
            if (mounted) {
              setHub(data);
              setError(null);
            }
          } catch (localErr) {
            if (mounted) setError(localErr.message);
          }
        } else if (mounted) {
          setError(err.message);
        }
      } finally {
        if (mounted) {
          clearTimeout(timeoutId);
          setLoading(false);
        }
      }
    }

    load();

    return () => {
      mounted = false;
      clearTimeout(timeoutId);
      controller.abort();
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        <div className="skeleton-title" />
        <div className="skeleton-list" />
      </div>
    );
  }

  if (error || !hub) {
    return (
      <div
        className="top-hub-panel empty"
        title="Enable knowledge-rag to populate signals"
        aria-live="polite"
      >
        <p className="empty-title">No hub signals available</p>
        <p className="empty-hint">Run knowledge-rag to generate top-hub insights.</p>
      </div>
    );
  }

  return (
    <div className="top-hub-panel" aria-label={`Top hub: ${hub.label}`}>
      <header className="top-hub-panel__header">
        <div>
          <h3 className="top-hub-panel__title">{hub.label}</h3>
          <p className="top-hub-panel__subtitle">{hub.description}</p>
        </div>
        <span className="top-hub-panel__badge">Top Hub</span>
      </header>

      <section className="top-hub-panel__proposals" aria-label="Top proposals">
        {hub.proposals?.slice(0, 3).map((p) => (
          <article key={p.id} className="proposal-card">
            <div className="proposal-card__header">
              <span className="proposal-card__title">{p.title}</span>
              <span
                className="proposal-card__impact"
                style={{ color: impactColor[p.impact] || 'inherit' }}
              >
                {p.impact}
              </span>
            </div>
            <p className="proposal-card__signal">{p.signal}</p>
            <div className="proposal-card__footer">
              <span className="proposal-card__effort">{p.effort}</span>
              <button
                type="button"
                className="proposal-card__cta"
                onClick={() => {
                  // Handoff to change management / proposals page (sense only)
                  window.dispatchEvent(
                    new CustomEvent('costinel:proposal:select', { detail: p })
                  );
                }}
              >
                {p.cta}
              </
