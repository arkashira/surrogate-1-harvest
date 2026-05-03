# Costinel / frontend

## Final Synthesis — Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — CDN-first, rate-limit-safe, zero API calls during render.

### Why this is highest value
- Directly applies past patterns (#knowledge-rag, #top-hub) and the HF CDN bypass insight.
- Read-only, non-breaking, visible immediately on the main dashboard.
- Pure frontend addition (React + Tailwind). No backend changes, no runtime HF API calls.
- Can ship in ~90–110 min with minimal code.

---

## Implementation Plan (merged + corrected)

1. **Create CDN-ready signal file** (commit to repo; served via CDN)  
   - Path: `public/knowledge/hubs/moc.json`  
   - Shape:
     ```json
     {
       "hub": "MOC",
       "description": "Mission Operations Center — central coordination for cloud ops",
       "updatedAt": "2026-05-03T02:30:00Z",
       "signals": [
         {
           "id": "moc-ri-coverage-gap",
           "title": "RI coverage gap in us-east-1",
           "impact": "high",
           "estimatedSavingsUSD": 42000,
           "action": "Purchase 1yr Standard RIs for steady-state EKS nodes",
           "rationale": "78% of steady workloads run on-demand; 12-month ROI < 4 months"
         },
         {
           "id": "moc-orphaned-snapshots",
           "title": "Orphaned EBS snapshots > 90 days",
           "impact": "medium",
           "estimatedSavingsUSD": 8400,
           "action": "Apply snapshot lifecycle policy and delete unreferenced snapshots",
           "rationale": "230 snapshots, 14 TB across 4 accounts"
         },
         {
           "id": "moc-idle-nat-gateways",
           "title": "Idle NAT gateways in dev accounts",
           "impact": "medium",
           "estimatedSavingsUSD": 3600,
           "action": "Schedule NAT gateways off during non-business hours",
           "rationale": "6 idle NATs costing ~$300/mo each"
         }
       ]
     }
     ```
   - Rationale field replaces “context” for clarity and consistency with doc patterns.

2. **Add SignalPanel component** (`src/components/SignalPanel.jsx`)  
   - CDN-first fetch in `useEffect` (or `getStaticProps` if SSR).  
   - Fallback to bundled defaults if fetch fails.  
   - Cache with `stale-while-revalidate` (or `Cache-Control`) to avoid runtime rate limits.  
   - Zero Authorization headers; rely on public CDN.

3. **Wire into dashboard layout**  
   - Insert `<SignalPanel />` near the top of the main dashboard view (below header or in sidebar, depending on current layout).

4. **Styling**  
   - Use existing design tokens (colors, spacing) to keep consistent with Costinel UI.  
   - Tailwind classes preferred; minimal custom CSS only where necessary.

---

## Final Code Snippets

### `src/components/SignalPanel.jsx`
```jsx
import { useEffect, useState } from "react";
import "./SignalPanel.css";

const DEFAULT_HUB = {
  hub: "MOC",
  description: "Mission Operations Center — central coordination for cloud ops",
  updatedAt: new Date().toISOString(),
  signals: [
    {
      id: "moc-ri-coverage-gap",
      title: "RI coverage gap in us-east-1",
      impact: "high",
      estimatedSavingsUSD: 42000,
      action: "Purchase 1yr Standard RIs for steady-state EKS nodes",
      rationale: "78% of steady workloads run on-demand; 12-month ROI < 4 months",
    },
    {
      id: "moc-orphaned-snapshots",
      title: "Orphaned EBS snapshots > 90 days",
      impact: "medium",
      estimatedSavingsUSD: 8400,
      action: "Apply snapshot lifecycle policy and delete unreferenced snapshots",
      rationale: "230 snapshots, 14 TB across 4 accounts",
    },
    {
      id: "moc-idle-nat-gateways",
      title: "Idle NAT gateways in dev accounts",
      impact: "medium",
      estimatedSavingsUSD: 3600,
      action: "Schedule NAT gateways off during non-business hours",
      rationale: "6 idle NATs costing ~$300/mo each",
    },
  ],
};

const impactColor = (impact) => {
  switch (impact) {
    case "high":
      return "var(--impact-high, #ef4444)";
    case "medium":
      return "var(--impact-medium, #f59e0b)";
    default:
      return "var(--impact-low, #10b981)";
  }
};

export default function SignalPanel() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first: try public JSON; fallback to default
    fetch("/knowledge/hubs/moc.json", { cache: "force-cache" })
      .then((res) => {
        if (!res.ok) throw new Error("CDN fetch failed");
        return res.json();
      })
      .then((data) => {
        setHub(data);
        setLoading(false);
      })
      .catch(() => {
        // Use default hub data
        setHub(DEFAULT_HUB);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return <div className="signal-panel loading">Loading signals…</div>;
  }

  if (!hub) return null;

  return (
    <div className="signal-panel">
      <div className="signal-panel__header">
        <h3 className="signal-panel__title">Top Hub: {hub.hub}</h3>
        <p className="signal-panel__desc">{hub.description}</p>
        <small className="signal-panel__updated">
          Updated {new Date(hub.updatedAt).toLocaleDateString()}
        </small>
      </div>

      <div className="signal-panel__signals">
        {hub.signals.map((s) => (
          <div key={s.id} className="signal-card">
            <div className="signal-card__header">
              <span
                className="signal-card__impact"
                style={{ backgroundColor: impactColor(s.impact) }}
              >
                {s.impact}
              </span>
              <span className="signal-card__savings">
                Save ${s.estimatedSavingsUSD.toLocaleString()}
              </span>
            </div>
            <h4 className="signal-card__title">{s.title}</h4>
            <p className="signal-card__action">{s.action}</p>
            <p className="signal-card__rationale">{s.rationale}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
```

### `src/components/SignalPanel.css`
```css
.signal-panel {
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  padding: 16px;
  background: var(--bg-panel, #fff);
  margin-bottom: 16px;
}

.signal-panel__header {
  margin-bottom: 12px;
}

.signal-panel__title {
  margin: 0 0 4px 0;
  font-size: 1.125rem;
  font-weight: 600;
}

.signal-panel__desc {
  margin: 0 0 4px 0;
  color: var(--text-muted, #6b7280);
  font-size: 0.875rem;
}

.signal-panel__updated {
  color: var(--text-muted, #6b7280);
