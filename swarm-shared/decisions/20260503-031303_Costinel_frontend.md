# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default: `MOC`) with 3 contextual insights from the knowledge graph. Uses CDN-first pattern (bypasses HF API during render) and reuses existing patterns.

### Architecture decisions
- **CDN-first**: insights JSON lives at `public/knowledge-rag/top-hub/{hub}/insights.json` → fetch at runtime (zero API cost, no backend changes).
- **Non-blocking**: panel renders instantly with cached data; background refresh optional.
- **No build step**: static JSON + client-side render keeps changes minimal and <2h.
- **Extensible**: hub name configurable via `data-hub` attribute for future multi-hub.

### Files to create/modify
1. `public/knowledge-rag/top-hub/MOC/insights.json` — static insights payload.
2. `src/components/TopHubSignalPanel.jsx` — React component (or `.tsx` if TS project).
3. `src/components/TopHubSignalPanel.css` — minimal styles.
4. Integrate into dashboard: `src/pages/Dashboard.jsx` (or equivalent).

### insights.json schema
```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "score": 94,
  "insights": [
    {
      "id": "i1",
      "headline": "Cost variance +12% vs plan",
      "detail": "Compute over-provisioning in us-east-1 detected across 3 accounts",
      "severity": "warning",
      "action": "Review RI coverage"
    },
    {
      "id": "i2",
      "headline": "Anomaly: idle GPU clusters",
      "detail": "2 clusters idle >72h; estimated $4.2k/mo waste",
      "severity": "critical",
      "action": "Schedule stop or downsize"
    },
    {
      "id": "i3",
      "headline": "Signal: forecast breach risk",
      "detail": "On-track spend exceeds budget by 8% in 14 days without intervention",
      "severity": "warning",
      "action": "Apply governance controls"
    }
  ],
  "updatedAt": "2026-05-03T03:09:00.000Z"
}
```

### Component implementation (React)

`src/components/TopHubSignalPanel.jsx`
```jsx
import React, { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

export default function TopHubSignalPanel({ hub = "MOC", cdnBase = "/knowledge-rag/top-hub" }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const url = `${cdnBase}/${hub}/insights.json?cb=${Date.now()}`;
    fetch(url, { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load hub insights: ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData(json);
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [hub, cdnBase]);

  if (loading) {
    return (
      <div className="top-hub-panel loading" data-testid="top-hub-loading">
        <div className="panel-header">
          <div className="hub-badge shimmer" />
        </div>
        <div className="insights-list">
          {[1, 2, 3].map((k) => (
            <div key={k} className="insight-row shimmer" />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    // Non-blocking: render minimal fallback
    return (
      <div className="top-hub-panel error" data-testid="top-hub-error">
        <small>Signal unavailable</small>
      </div>
    );
  }

  const severityClass = (s) => `severity-${s}`;

  return (
    <div className="top-hub-panel" data-hub={data.hub} data-testid="top-hub-panel">
      <div className="panel-header">
        <div className="hub-badge">{data.hub}</div>
        <div className="hub-label">{data.label}</div>
        <div className="hub-score" title="Connectedness score">
          {data.score}
        </div>
      </div>

      <div className="insights-list">
        {data.insights.map((ins) => (
          <div key={ins.id} className={`insight-row ${severityClass(ins.severity)}`}>
            <div className="insight-headline">{ins.headline}</div>
            <div className="insight-detail">{ins.detail}</div>
            <div className="insight-action">{ins.action}</div>
          </div>
        ))}
      </div>

      <div className="panel-footer">
        <small>Updated {new Date(data.updatedAt).toLocaleDateString()}</small>
      </div>
    </div>
  );
}
```

`src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

.panel-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 10px;
}

.hub-badge {
  font-weight: 700;
  font-size: 14px;
  color: #0b5cff;
  letter-spacing: 0.02em;
}

.hub-label {
  font-size: 13px;
  color: #6b7280;
}

.hub-score {
  margin-left: auto;
  font-size: 14px;
  font-weight: 600;
  color: #374151;
  background: #f3f4f6;
  padding: 2px 8px;
  border-radius: 99px;
}

.insights-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.insight-row {
  padding: 8px 10px;
  border-radius: 6px;
  border-left: 3px solid #9ca3af;
  background: #fafafa;
  font-size: 13px;
}

.insight-row.severity-critical {
  border-left-color: #dc2626;
  background: #fef2f2;
}

.insight-row.severity-warning {
  border-left-color: #f59e0b;
  background: #fffbeb;
}

.insight-headline {
  font-weight: 600;
  color: #111827;
  margin-bottom: 2px;
}

.insight-detail {
  color: #4b5563;
  margin-bottom: 2px;
}

.insight-action {
  font-size: 12px;
  color: #0b5cff;
  cursor: pointer;
}

.panel-footer {
  margin-top: 8px;
  text-align: right;
  color: #9ca3af;
  font-size: 11px;
}

/* shimmer placeholders */
.shimmer {
  background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
  background-size
