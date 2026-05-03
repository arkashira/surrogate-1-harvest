# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (≤2h)

**Scope**: Frontend-only, read-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces it with 3 contextual signals.  
**Pattern**: Sense + Signal (no execution).  
**Timebox**: ≤2 hours.  
**Rollout**: Toggleable via feature flag for staged release.

---

### 1) Highest-value improvement (merged rationale)
Embed a lightweight “Top-Hub Signal” card into the Costinel dashboard that:
- Reads a **single, purpose-built JSON artifact** (derived from the knowledge-rag graph) from `/public/knowledge-rag/`.
- Identifies the most-connected hub and renders it with **title, description, and three contextual signals** (title + snippet + link).
- Uses existing design tokens, is zero-backend, and demonstrates “Sense + Signal” without execution.

**Why this wins**:
- Combines Candidate 1’s graph-based degree computation (for correctness) with Candidate 2’s ready-to-render signal schema (for concrete actionability).
- Avoids runtime graph parsing in production by baking the computed top hub into the artifact, keeping the 2h budget realistic.
- Toggleable via feature flag enables safe rollout.

---

### 2) Concrete implementation

#### A) Static artifact (mock → prod path)
Path: `/public/knowledge-rag/knowledge-rag-top-hub.json`

```json
{
  "hub": {
    "id": "MOC",
    "label": "MOC",
    "type": "hub",
    "connections": 142,
    "description": "Master Operating Context — central reference model for cost governance decisions."
  },
  "signals": [
    {
      "id": "s1",
      "title": "RI Coverage Gap in us-east-1",
      "snippet": "Detected 34% RI coverage shortfall for m5 family; estimated $18k/mo savings opportunity.",
      "link": "/insights/ri-coverage-gap",
      "type": "recommendation"
    },
    {
      "id": "s2",
      "title": "Anomalous Data Transfer Spike",
      "snippet": "Cross-AZ traffic increased 210% vs baseline; review VPC endpoints and NAT costs.",
      "link": "/anomalies/data-transfer",
      "type": "anomaly"
    },
    {
      "id": "s3",
      "title": "Commit Cap 128/hr Breach Risk",
      "snippet": "HF Commit Cap at 128/hr is a binding constraint; stagger jobs or request quota increase.",
      "link": "/constraints/commit-cap-128",
      "type": "constraint"
    }
  ],
  "_meta": {
    "generatedAt": "2025-06-01T00:00:00Z",
    "sourceGraph": "knowledge-rag-graph.json",
    "topHubBy": "degree"
  }
}
```

#### B) Card component (React/TSX)
Path: `src/components/TopHubSignalCard.tsx`

```tsx
import { useEffect, useState } from "react";
import "./TopHubSignalCard.css";

interface Hub {
  id: string;
  label: string;
  type: "hub";
  connections: number;
  description: string;
}

interface Signal {
  id: string;
  title: string;
  snippet: string;
  link: string;
  type: "recommendation" | "anomaly" | "constraint";
}

interface TopHubPayload {
  hub: Hub;
  signals: Signal[];
  _meta?: { generatedAt: string; sourceGraph: string; topHubBy: string };
}

export default function TopHubSignalCard() {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/knowledge-rag/knowledge-rag-top-hub.json")
      .then((r) => r.json())
      .then((data) => setPayload(data))
      .catch((err) => console.error("Failed to load Top-Hub Signal", err))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="top-hub-card loading" aria-busy="true">
        <div className="skeleton hub-skeleton" />
        <div className="skeleton signal-skeleton" />
        <div className="skeleton signal-skeleton" />
        <div className="skeleton signal-skeleton" />
      </div>
    );
  }

  if (!payload) {
    return null;
  }

  const { hub, signals } = payload;

  return (
    <div className="top-hub-card" role="region" aria-label={`Top hub: ${hub.label}`}>
      <div className="hub-header">
        <span className="hub-badge">HUB</span>
        <h3 className="hub-name">{hub.label}</h3>
        <p className="hub-sub">{hub.description}</p>
        <div className="hub-meta">
          <span>{hub.connections} connections</span>
        </div>
      </div>

      <div className="signals-list">
        {signals.map((s) => (
          <a key={s.id} href={s.link} className="signal-item" target="_self" rel="noopener">
            <span className={`signal-dot ${s.type}`} />
            <div className="signal-content">
              <div className="signal-title">{s.title}</div>
              <div className="signal-snippet">{s.snippet}</div>
            </div>
          </a>
        ))}
      </div>

      <div className="card-footer">
        <small>Sense + Signal — no execution</small>
      </div>
    </div>
  );
}
```

#### C) Minimal CSS
Path: `src/components/TopHubSignalCard.css`

```css
.top-hub-card {
  border: 1px solid #e6eef8;
  border-radius: 12px;
  padding: 16px;
  background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.04), 0 1px 2px rgba(16, 24, 40, 0.06);
}

.hub-header {
  margin-bottom: 12px;
}

.hub-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: #2563eb;
  background: #eff6ff;
  padding: 2px 6px;
  border-radius: 4px;
  margin-bottom: 4px;
}

.hub-name {
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
  margin: 0;
}

.hub-sub {
  font-size: 12px;
  color: #64748b;
  margin: 2px 0 0;
}

.hub-meta {
  font-size: 11px;
  color: #94a3b8;
  margin-top: 4px;
}

.signals-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.signal-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  color: #334155;
  text-decoration: none;
  padding: 6px 0;
  border-bottom: 1px solid #f1f5f9;
}

.signal-item:last-child {
  border-bottom: none;
}

.signal-dot {
  width: 8px;
  height: 8px;
  border-radius
