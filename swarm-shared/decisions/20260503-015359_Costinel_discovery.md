# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- Ships in <2h as a resilient, self-contained widget that can be dropped into the existing Costinel dashboard without backend changes.  
- Follows past patterns: review top-hub before planning; use knowledge-rag insights; avoid execution (Sense + Signal).

---

### 1. Implementation Steps (≤2h)

1. **Locate dashboard layout**  
   Identify the main dashboard component (likely `src/pages/Dashboard.tsx` or `src/components/Dashboard/Dashboard.tsx`). Add a new panel slot near cost-summary or recommendations.

2. **Create TopHubSignalPanel component**  
   - File: `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`  
   - Props: `hubName?: string` (default `"MOC"`), `maxSignals?: number` (default `5`).  
   - Behavior: static data source first (JSON), wired later to knowledge-rag API.  
   - UI: card with hub title, short description, list of signals (proposal title, impact, confidence, tags), and a “View in Graph” link.

3. **Add static dataset**  
   - File: `src/data/top-hub-signals.json`  
   - Shape: `{ hub: "MOC", signals: [{ id, title, impact, confidence, tags, href }] }`  
   - Seed with 3–5 high-value MOC-related cost-governance proposals (e.g., RI coverage gaps, orphaned resources, budget alerts).

4. **Integrate into dashboard**  
   Import and render `TopHubSignalPanel` in the dashboard grid. Ensure responsive layout (mobile-first).

5. **Styling & polish**  
   Use existing design tokens (colors, spacing, typography). Add subtle icon for hub and confidence badges. Ensure accessibility (aria labels, focus states).

6. **Validation**  
   - Run dev server and verify panel renders without errors.  
   - Confirm no console warnings.  
   - Check responsive breakpoints.

---

### 2. Code Snippets

#### `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`
```tsx
import React from "react";
import { Signal } from "../../types";
import "./TopHubSignalPanel.css";

interface TopHubSignalPanelProps {
  hubName?: string;
  maxSignals?: number;
  signals?: Signal[];
}

const defaultSignals: Signal[] = [
  {
    id: "moc-ri-gap",
    title: "Reduce RI coverage gap in us-east-1",
    impact: "High",
    confidence: 0.92,
    tags: ["RI", "AWS", "Cost-Optimization"],
    href: "/proposals/moc-ri-gap",
  },
  {
    id: "moc-orphaned-volumes",
    title: "Detach orphaned EBS volumes (>30d idle)",
    impact: "Medium",
    confidence: 0.85,
    tags: ["Storage", "AWS", "Cleanup"],
    href: "/proposals/moc-orphaned-volumes",
  },
  {
    id: "moc-budget-alert",
    title: "Update budget alert thresholds for Q3",
    impact: "Medium",
    confidence: 0.78,
    tags: ["Budget", "Governance"],
    href: "/proposals/moc-budget-alert",
  },
];

export const TopHubSignalPanel: React.FC<TopHubSignalPanelProps> = ({
  hubName = "MOC",
  maxSignals = 5,
  signals = defaultSignals,
}) => {
  const displayed = signals.slice(0, maxSignals);

  return (
    <div className="top-hub-signal-panel" role="region" aria-label={`Top hub: ${hubName} signals`}>
      <div className="panel-header">
        <h3 className="hub-title">{hubName}</h3>
        <p className="hub-subtitle">Top actionable signals from knowledge graph</p>
      </div>

      <ul className="signals-list" aria-live="polite">
        {displayed.map((s) => (
          <li key={s.id} className="signal-item">
            <a href={s.href} className="signal-link">
              <div className="signal-title">{s.title}</div>
              <div className="signal-meta">
                <span className={`impact-badge impact-${s.impact.toLowerCase()}`}>{s.impact}</span>
                <span className="confidence">{Math.round(s.confidence * 100)}%</span>
              </div>
              <div className="signal-tags">
                {s.tags.map((t) => (
                  <span key={t} className="tag">
                    {t}
                  </span>
                ))}
              </div>
            </a>
          </li>
        ))}
      </ul>

      <div className="panel-footer">
        <a href="/knowledge-graph" className="view-graph-link">
          View in Graph →
        </a>
      </div>
    </div>
  );
};
```

#### `src/components/TopHubSignalPanel/TopHubSignalPanel.css`
```css
.top-hub-signal-panel {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 260px;
}

.panel-header .hub-title {
  font-size: 18px;
  font-weight: 600;
  margin: 0;
  color: #111827;
}

.panel-header .hub-subtitle {
  font-size: 12px;
  color: #6b7280;
  margin: 2px 0 0;
}

.signals-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
}

.signal-item {
  border-radius: 6px;
  padding: 8px;
  transition: background 0.15s;
}

.signal-item:hover {
  background: #f9fafb;
}

.signal-link {
  text-decoration: none;
  color: inherit;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.signal-title {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
  line-height: 1.4;
}

.signal-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 11px;
}

.impact-badge {
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.02em;
}

.impact-high {
  background: #fee2e2;
  color: #991b1b;
}

.impact-medium {
  background: #fef3c7;
  color: #92400e;
}

.impact-low {
  background: #dcfce7;
  color: #166534;
}

.confidence {
  color: #6b7280;
}

.signal-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag {
  font-size: 10px;
  background: #e5e7eb;
  color: #374151;
  padding: 2px 6px;
  border-radius: 4px;
}

.panel-footer {
  border-top: 1px solid #f3f4f6;
  padding-top: 8px;
  text-align: right;
}

.view-graph-link {
