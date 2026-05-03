# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data path (no backend calls). Ships in <2h.

---

### 1) File changes
- `src/components/dashboard/TopHubSignalPanel.tsx` — new component  
- `src/components/dashboard/TopHubSignalPanel.css` — styles  
- `src/pages/Dashboard.tsx` — mount panel near top of dashboard  
- `public/data/knowledge-graph/top-hub-signals.json` — static CDN payload (sample)

---

### 2) Data contract (CDN JSON)

Use a single, canonical contract that merges the strongest fields and normalizes impact to an actionable numeric value.

`public/data/knowledge-graph/top-hub-signals.json`:

```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "description": "Central hub for cloud governance signals and cross-account policies",
  "lastUpdated": "2026-05-03T02:14:42Z",
  "proposals": [
    {
      "id": "prop-001",
      "title": "Shift 30% dev EKS nodes to Spot + RI mix",
      "impactUsd": 216000,
      "timeframe": "Q3",
      "confidence": 0.87,
      "tags": ["AWS", "EKS", "RI", "Spot"],
      "rationale": "High idle hours + predictable baseline → RI+Spot blend cuts run cost 38%"
    },
    {
      "id": "prop-002",
      "title": "Enforce storage tiering for cold snapshots (S3 → Glacier Deep)",
      "impactUsd": 84000,
      "timeframe": "Q2",
      "confidence": 0.79,
      "tags": ["AWS", "S3", "Lifecycle"],
      "rationale": "90-day retention snapshots are 95% cold; tiering reduces storage cost 62%"
    },
    {
      "id": "prop-003",
      "title": "Right-size over-provisioned GKE node pools (CPU <30%)",
      "impactUsd": 38000,
      "timeframe": "Q2",
      "confidence": 0.74,
      "tags": ["GCP", "GKE", "RightSize"],
      "rationale": "Steady low CPU + high allocatable → 25% node count reduction safe"
    }
  ]
}
```

Key choices:
- `impactUsd` is annualized USD (clear, numeric, sortable).  
- Keep `timeframe`, `confidence`, `rationale`, and `tags` for actionability.  
- `lastUpdated` supports cache-busting UX.

---

### 3) Component implementation

`src/components/dashboard/TopHubSignalPanel.tsx`:

```tsx
import React, { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

interface Proposal {
  id: string;
  title: string;
  impactUsd: number;
  timeframe: string;
  confidence: number;
  tags: string[];
  rationale: string;
}

interface TopHubPayload {
  hub: string;
  label: string;
  description: string;
  lastUpdated: string;
  proposals: Proposal[];
}

const CDN_URL = `${process.env.PUBLIC_URL}/data/knowledge-graph/top-hub-signals.json`;

const TopHubSignalPanel: React.FC = () => {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(CDN_URL, { cache: "no-cache" })
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load hub signals");
        return res.json();
      })
      .then((json) => {
        setPayload(json);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        Loading signals…
      </div>
    );
  }

  if (error || !payload) {
    return (
      <div className="top-hub-panel error" role="alert">
        Unable to load signals.
      </div>
    );
  }

  return (
    <section className="top-hub-panel" aria-label={`Top hub: ${payload.hub}`}>
      <header className="top-hub-header">
        <div className="top-hub-badge">{payload.hub}</div>
        <div className="top-hub-head-text">
          <h2 className="top-hub-title">{payload.label}</h2>
          <p className="top-hub-desc">{payload.description}</p>
        </div>
      </header>

      <div className="top-hub-proposals" role="list">
        {payload.proposals.map((p) => (
          <article key={p.id} className="proposal-card" role="listitem">
            <div className="proposal-head">
              <h3 className="proposal-title">{p.title}</h3>
              <span className="proposal-impact">
                Save <strong>${Math.round(p.impactUsd).toLocaleString()}</strong>/yr
              </span>
            </div>
            <p className="proposal-rationale">{p.rationale}</p>
            <div className="proposal-meta">
              <div className="tag-list" role="list">
                {p.tags.map((t) => (
                  <span key={t} className="tag" role="listitem">
                    {t}
                  </span>
                ))}
              </div>
              <div className="meta-aux">
                <span className="confidence">Confidence {(p.confidence * 100).toFixed(0)}%</span>
                <span className="timeframe">{p.timeframe}</span>
              </div>
            </div>
          </article>
        ))}
      </div>

      <footer className="top-hub-footer">
        <small>Last updated {new Date(payload.lastUpdated).toLocaleDateString()}</small>
      </footer>
    </section>
  );
};

export default TopHubSignalPanel;
```

---

### 4) Styles

`src/components/dashboard/TopHubSignalPanel.css`:

```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 12px;
  padding: 20px 24px;
  background: #fff;
  max-width: 900px;
  margin: 0 auto 24px;
}

.top-hub-panel.loading,
.top-hub-panel.error {
  color: #6b7280;
  text-align: center;
  padding: 32px;
}

.top-hub-header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 16px;
}

.top-hub-badge {
  background: #1e40af;
  color: #fff;
  font-weight: 700;
  font-size: 14px;
  padding: 6px 10px;
  border-radius: 6px;
  min-width: 56px;
  text-align: center;
  flex-shrink: 0;
}

.top-hub-head-text {
  flex: 1;
  min-width: 0;
}

.top-hub-title {
  font-size: 18px;
  margin: 0;
  color: #0f172a;
}

.top-hub-desc {
  margin
