# Costinel / discovery

**Final Implementation Plan — Costinel “Top-Hub Signal” Card**  
*(2-hour frontend-only task, read-only, graceful fallback, React + TypeScript)*

---

### Core Decisions (merged + resolved)
- **Use React + TypeScript** (Candidate 2) for maintainability and alignment with existing dashboard patterns.  
- **Degree = total connections** (in + out) (Candidate 2) — correct and simplest for “top hub.”  
- **Top 3 contextual signals = highest-weight connected nodes** (Candidate 2) — more actionable than arbitrary picks.  
- **Static JSON mock** (Candidate 2) to unblock frontend work; replaceable later by real endpoint.  
- **Graceful fallback** (both): show neutral empty state, no errors, no layout shift.  
- **No backend/infra/auth changes** (both).  

---

### Deliverables (≤2h)
1. `src/components/cards/TopHubSignalCard.tsx` — reusable card component.  
2. `src/mocks/knowledgeRagGraph.json` — mock graph payload.  
3. Integrate card into dashboard grid (e.g., `src/pages/Dashboard.tsx`).  
4. Minimal CSS consistent with design tokens.  

---

### File: `src/mocks/knowledgeRagGraph.json`
```json
{
  "generatedAt": "2026-05-03T04:45:00Z",
  "nodes": [
    { "id": "MOC", "label": "MOC", "type": "hub", "description": "Mission Operations Center — central coordination for cloud ops" },
    { "id": "RI", "label": "Reserved Instances", "type": "concept" },
    { "id": "Forecast", "label": "Cost Forecasting", "type": "concept" },
    { "id": "Anomaly", "label": "Anomaly Detection", "type": "concept" },
    { "id": "Governance", "label": "Governance Policies", "type": "concept" }
  ],
  "edges": [
    { "source": "MOC", "target": "RI", "weight": 8 },
    { "source": "MOC", "target": "Forecast", "weight": 6 },
    { "source": "MOC", "target": "Anomaly", "weight": 7 },
    { "source": "MOC", "target": "Governance", "weight": 5 },
    { "source": "RI", "target": "Forecast", "weight": 3 }
  ]
}
```

---

### File: `src/components/cards/TopHubSignalCard.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import graphData from '../../mocks/knowledgeRagGraph.json';
import './TopHubSignalCard.css';

type Node = {
  id: string;
  label: string;
  type: string;
  description?: string;
};

type Edge = {
  source: string;
  target: string;
  weight: number;
};

type GraphPayload = {
  generatedAt: string;
  nodes: Node[];
  edges: Edge[];
};

const TopHubSignalCard: React.FC = () => {
  const [topHub, setTopHub] = useState<Node | null>(null);
  const [signals, setSignals] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    try {
      const data = graphData as GraphPayload;
      if (!data?.nodes || !data?.edges) {
        setError(true);
        return;
      }

      // Compute degree per node (in + out)
      const degree: Record<string, number> = {};
      data.nodes.forEach((n) => (degree[n.id] = 0));
      data.edges.forEach((e) => {
        degree[e.source] = (degree[e.source] || 0) + 1;
        degree[e.target] = (degree[e.target] || 0) + 1;
      });

      const topNodeId = Object.entries(degree).sort((a, b) => b[1] - a[1])[0]?.[0];
      const hubNode = data.nodes.find((n) => n.id === topNodeId) || null;

      // Top 3 signals: highest-weight connected nodes
      const connectedEdges = data.edges.filter(
        (e) => e.source === topNodeId || e.target === topNodeId
      );
      const weighted = connectedEdges
        .map((e) => ({
          nodeId: e.source === topNodeId ? e.target : e.source,
          weight: e.weight
        }))
        .sort((a, b) => b.weight - a.weight);

      const top3 = weighted.slice(0, 3).map((item) => {
        const node = data.nodes.find((n) => n.id === item.nodeId);
        return node?.label || item.nodeId;
      });

      setTopHub(hubNode);
      setSignals(top3);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  if (loading) {
    return (
      <div className="top-hub-card card">
        <div className="card-header">
          <div className="skeleton skeleton-text" style={{ width: '40%' }} />
        </div>
        <div className="card-body">
          <div className="skeleton skeleton-text" style={{ width: '80%' }} />
          <div className="signals">
            {[...Array(3)].map((_, i) => (
              <div key={i} className="skeleton skeleton-chip" />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (error || !topHub) {
    return (
      <div className="top-hub-card card empty-state">
        <div className="empty-content">
          <span className="empty-icon">—</span>
          <p>No graph signals available</p>
        </div>
      </div>
    );
  }

  return (
    <div className="top-hub-card card">
      <div className="card-header">
        <div className="hub-title">
          <span className="hub-icon">★</span>
          <h3>{topHub.label}</h3>
        </div>
      </div>
      <div className="card-body">
        <p className="hub-desc">{topHub.description || 'Central knowledge hub'}</p>
        <div className="signals">
          {signals.map((s, i) => (
            <span key={i} className="signal-chip">
              {s}
            </span>
          ))}
        </div>
        <p className="updated">
          Updated {new Date(graphData.generatedAt).toLocaleDateString()}
        </p>
      </div>
    </div>
  );
};

export default TopHubSignalCard;
```

---

### File: `src/components/cards/TopHubSignalCard.css`
```css
.top-hub-card.card {
  padding: 16px;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #e6e9ee;
  min-height: 140px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.card-header .hub-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.hub-icon {
  font-size: 18px;
}

.card-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
}

.hub-desc {
  margin: 0;
  font-size: 13px;
  color: #556;
}

.signals {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.signal-chip {
  background: #f0f4f8;
  color: #243449;
  padding: 4px 8px;
  border-radius: 999px;
 
