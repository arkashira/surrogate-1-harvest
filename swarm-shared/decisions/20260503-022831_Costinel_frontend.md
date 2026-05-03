# Costinel / frontend

## Highest-Value Incremental Improvement (frontend)

**Goal**: Ship a read-only **Signal Panel** (top-hub insights widget) that shows the most-connected hub (e.g., "MOC") and related contextual insights from the knowledge-rag pipeline — CDN-first, zero backend changes, <2h.

**Why this**:  
- Directly applies #knowledge-rag #hub patterns.  
- Visible value immediately (dashboard context for cost governance).  
- CDN-first avoids API rate limits and keeps frontend autonomy.  
- Read-only, so no backend/security surface.

---

## Implementation Plan (≤2h)

1. **Add Signal Panel component** (`src/components/SignalPanel.tsx`)  
   - Fetch precomputed top-hub + insights JSON from CDN (`/data/signal/top-hub.json`).  
   - Render card: hub name, short description, related doc links, last-updated timestamp.  
   - Graceful fallback UI if fetch fails or data missing.

2. **Add CDN data file** (`public/data/signal/top-hub.json`)  
   - Minimal schema: `{ hub, description, insights: [{ title, url, snippet }], updatedAt }`.  
   - Commit once; later updated by knowledge-rag pipeline (out of scope for this frontend task).

3. **Wire into dashboard** (`src/pages/Dashboard.tsx` or main layout)  
   - Place Signal Panel in the cost-visibility sidebar or top banner area.  
   - Mobile-responsive, compact by default, expandable for details.

4. **Styling & polish**  
   - Use existing design tokens (colors, spacing).  
   - Add subtle icon (hub/insight) and loading skeleton.

5. **Build & smoke test**  
   - `npm run build` → verify no runtime errors and CDN fetch works in production build.

---

## Code Snippets

### `public/data/signal/top-hub.json`
```json
{
  "hub": "MOC",
  "description": "Most-connected operational cost hub — central to anomaly detection and governance signals.",
  "insights": [
    {
      "title": "Q2 cost anomalies in MOC-linked services",
      "url": "https://docs.axentx/costinel/signals/moc-q2-anomalies",
      "snippet": "Three recurring spikes tied to cross-region replication."
    },
    {
      "title": "MOC coverage recommendations",
      "url": "https://docs.axentx/costinel/recommendations/moc-ri-coverage",
      "snippet": "Increase reserved coverage to 65% to reduce run-rate by ~12%."
    }
  ],
  "updatedAt": "2026-05-03T08:00:00Z"
}
```

---

### `src/components/SignalPanel.tsx`
```tsx
import React, { useEffect, useState } from "react";
import "./SignalPanel.css";

interface Insight {
  title: string;
  url: string;
  snippet: string;
}

interface TopHubData {
  hub: string;
  description: string;
  insights: Insight[];
  updatedAt: string;
}

const CDN_URL = "/data/signal/top-hub.json";

const SignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch(CDN_URL, { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error("Failed to fetch signal data");
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch(() => {
        setError(true);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="signal-panel loading">
        <div className="skeleton hub"></div>
        <div className="skeleton desc"></div>
        <div className="skeleton insight"></div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="signal-panel empty">
        <span>Signal unavailable</span>
      </div>
    );
  }

  return (
    <div className="signal-panel">
      <div className="signal-header">
        <span className="signal-badge">Top Hub</span>
        <strong className="hub-name">{data.hub}</strong>
        <p className="hub-desc">{data.description}</p>
        <time className="updated-at" dateTime={data.updatedAt}>
          Updated {new Date(data.updatedAt).toLocaleDateString()}
        </time>
      </div>

      <div className="insights-list">
        {data.insights.map((insight, idx) => (
          <a
            key={idx}
            href={insight.url}
            target="_blank"
            rel="noopener noreferrer"
            className="insight-item"
          >
            <div className="insight-title">{insight.title}</div>
            <div className="insight-snippet">{insight.snippet}</div>
          </a>
        ))}
      </div>
    </div>
  );
};

export default SignalPanel;
```

---

### `src/components/SignalPanel.css`
```css
.signal-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 14px;
  background: #fff;
  max-width: 320px;
}

.signal-panel.loading .skeleton {
  background: #f0f0f0;
  border-radius: 4px;
  margin-bottom: 8px;
}
.signal-panel.loading .hub { height: 18px; width: 60%; }
.signal-panel.loading .desc { height: 12px; width: 90%; }
.signal-panel.loading .insight { height: 10px; width: 100%; }

.signal-header { margin-bottom: 10px; }
.signal-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #0b66c3;
  background: #eef4fd;
  padding: 2px 6px;
  border-radius: 4px;
  margin-bottom: 6px;
}
.hub-name { font-size: 18px; margin-bottom: 4px; display: block; }
.hub-desc { margin: 0 0 6px; color: #556; font-size: 13px; }
.updated-at { font-size: 11px; color: #99a; }

.insights-list { display: flex; flex-direction: column; gap: 6px; }
.insight-item {
  display: block;
  padding: 8px 10px;
  border-radius: 6px;
  background: #fbfdff;
  border: 1px solid #eef4fd;
  text-decoration: none;
  color: inherit;
  transition: background 0.12s;
}
.insight-item:hover { background: #f0f6ff; }
.insight-title { font-weight: 600; font-size: 13px; color: #0b66c3; }
.insight-snippet { font-size: 12px; color: #667; margin-top: 2px; }
```

---

### Wire into dashboard (example)
```tsx
// src/pages/Dashboard.tsx (or layout)
import SignalPanel from "../components/SignalPanel";

export default function Dashboard() {
  return (
    <div className="dashboard-layout">
      <aside className="dashboard-sidebar">
        <SignalPanel />
        {/* other sidebar widgets */}
      </aside>
      <main className="dashboard-main">
        {/* existing cost analytics */}
      </main>
    </div>
  );
}
```


