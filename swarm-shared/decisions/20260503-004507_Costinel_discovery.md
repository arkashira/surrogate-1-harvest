# Costinel / discovery

Candidate 3:
## Implementation Plan — Costinel “Top-Hub Signal” Card (≤2h)

**Scope**: Pure-frontend, read-only card that surfaces the most-connected hub + 3 contextual signals from the knowledge-rag graph. Aligns with “Sense + Signal — ไม่ Execute”.

**Why this is highest-value**:  
- Directly applies past pattern `#top-hub doc insight` (2026-04-27) and `#knowledge-rag #graph #hub`.  
- Zero backend changes → safe to ship in <2h.  
- Immediate UX payoff: surfaces the most-connected hub (e.g., “MOC”) and 3 contextual signals for faster discovery decisions.

---

### 1) Implementation Steps

1. Add a new card component: `TopHubSignalCard` (React/TS).  
2. Fetch graph data from a static JSON endpoint (or local file) produced by knowledge-rag (e.g., `knowledge-rag/top-hub.json`).  
3. Identify the most-connected hub by highest degree/centrality.  
4. Render:  
   - Hub name + short description  
   - Top 3 contextual signals (title + snippet + link)  
   - Timestamp + source tag  
5. Place card on dashboard (Cost Analytics sidebar or top banner).  
6. Style to match existing design tokens and badge system.

---

### 2) Code Snippets

#### `src/components/TopHubSignalCard.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalCard.css';

interface Signal {
  title: string;
  snippet: string;
  href: string;
  source: string;
}

interface HubGraph {
  hub: string;
  description: string;
  degree: number;
  signals: Signal[];
  updatedAt: string;
}

const TopHubSignalCard: React.FC = () => {
  const [hub, setHub] = useState<HubGraph | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // knowledge-rag produced file (static or served via CDN)
    fetch('/knowledge-rag/top-hub.json')
      .then((r) => r.json())
      .then((data: HubGraph) => {
        setHub(data);
        setLoading(false);
      })
      .catch(() => {
        // graceful fallback
        setLoading(false);
      });
  }, []);

  if (loading) return <div className="hub-card loading">Loading signals…</div>;
  if (!hub || !hub.signals?.length) return null;

  const top3 = hub.signals.slice(0, 3);

  return (
    <div className="hub-card" role="region" aria-label="Top hub signal">
      <div className="hub-header">
        <span className="hub-badge">Top hub</span>
        <h3 className="hub-name">{hub.hub}</h3>
        <p className="hub-desc">{hub.description}</p>
        <small className="hub-meta">
          Degree: {hub.degree} · Updated {new Date(hub.updatedAt).toLocaleDateString()}
        </small>
      </div>

      <div className="hub-signals">
        {top3.map((s, i) => (
          <a key={i} className="signal-item" href={s.href} target="_blank" rel="noopener noreferrer">
            <strong>{s.title}</strong>
            <p>{s.snippet}</p>
            <span className="signal-source">{s.source}</span>
          </a>
        ))}
      </div>

      <div className="hub-footer">
        <span className="sense-tag">Sense + Signal — ไม่ Execute</span>
      </div>
    </div>
  );
};

export default TopHubSignalCard;
```

#### `src/components/TopHubSignalCard.css`
```css
.hub-card {
  border: 1px solid #e6eef8;
  border-radius: 10px;
  padding: 16px;
  background: #fbfdff;
  color: #1a2b3c;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
  max-width: 360px;
}

.hub-header .hub-badge {
  display: inline-block;
  background: #3b82f6;
  color: #fff;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
  margin-bottom: 8px;
}

.hub-name {
  margin: 6px 0 4px;
  font-size: 18px;
}

.hub-desc {
  margin: 0 0 6px;
  font-size: 13px;
  color: #475569;
}

.hub-meta {
  color: #64748b;
}

.hub-signals {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.signal-item {
  display: block;
  padding: 8px;
  border-radius: 6px;
  background: #fff;
  border: 1px solid #e2e8f0;
  text-decoration: none;
  color: inherit;
  transition: box-shadow 0.12s;
}

.signal-item:hover {
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}

.signal-item strong {
  font-size: 13px;
}

.signal-item p {
  margin: 4px 0 6px;
  font-size: 12px;
  color: #475569;
}

.signal-source {
  font-size: 11px;
  color: #94a3b8;
}

.hub-footer {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #e2e8f0;
  font-size: 11px;
  color: #64748b;
}

.sense-tag {
  color: #0ea5e9;
  font-weight: 600;
}
```

#### Sample `knowledge-rag/top-hub.json` (to be produced by knowledge-rag pipeline)
```json
{
  "hub": "MOC",
  "description": "Multi-cloud observability and cost governance center.",
  "degree": 42,
  "updatedAt": "2026-05-03T08:12:00Z",
  "signals": [
    {
      "title": "RI coverage gaps in us-east-1",
      "snippet": "Detected 34% RI underutilization across production accounts.",
      "href": "/insights/ri-coverage-us-east-1",
      "source": "Costinel Analyzer"
    },
    {
      "title": "Anomalous spend spike on EKS",
      "snippet": "Week-over-week increase of 22% traced to unoptimized node groups.",
      "href": "/anomalies/eks-spike-may",
      "source": "Costinel Sense"
    },
    {
      "title": "Tag compliance drift",
      "snippet": "12% of resources missing mandatory cost-center tag.",
      "href": "/compliance/tag-drift",
      "source": "Costinel Audit"
    }
  ]
}
```

---

### 3) Integration & Deployment

- Add `TopHubSignalCard` to the dashboard layout (e.g., sidebar or top banner) in `src/pages/Dashboard.tsx`.  
- Ensure `knowledge-rag/top-hub.json` is generated by the knowledge-rag pipeline and placed in `public/knowledge-rag/` (or served via CDN).  
- No backend changes required.  
- Verify card is read-only and contains no actionable buttons that perform mutations.

---

### 4) Acceptance Criteria

- Card renders the most-connected hub and 3 signals.  
