# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
- Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- CDN-first data: a single JSON file (`/data/knowledge-hubs/moc.json`) served from CDN (or local `public/data/`) for instant, cacheable loads with zero backend coupling.  
- Incremental: adds a dashboard widget and a dedicated route `/hubs/:hubId` for deeper exploration.  
- Ship time: ~90–120 minutes.

---

### File layout (relative to `/opt/axentx/Costinel`)

```
public/data/knowledge-hubs/
  └── moc.json
src/components/
  ├── HubSignalPanel.tsx      # dashboard widget (summary + link)
  ├── HubDetailPage.tsx       # route page for hub detail
  ├── TopHubSignalPanel.tsx   # detailed hub view (reusable)
  └── types/hub.ts            # TypeScript types
src/routes/
  └── hubRoutes.tsx           # route definition
src/App.tsx                   # register route
src/components/TopHubSignalPanel.css
```

---

### 1) Types (`src/types/hub.ts`)

```ts
export interface HubProposal {
  id: string;
  title: string;
  summary?: string;
  impact?: string;          // e.g. "-18% monthly run-rate"
  priority: 'critical' | 'high' | 'medium' | 'low';
  signalScore: number;      // 0..100
  confidence?: number;      // 0..1
  tags: string[];
  actions: Array<{
    label: string;
    href?: string;
    handler?: () => void;
    type?: 'internal' | 'external';
  }>;
  deadline?: string;        // ISO
  updatedAt: string;        // ISO
}

export interface HubSignal {
  id: string;
  type: 'anomaly' | 'recommendation' | 'info';
  severity: 'high' | 'medium' | 'low' | 'info';
  title: string;
  value?: string;
  timestamp: string;        // ISO
  tags: string[];
}

export interface KnowledgeHub {
  hubId: string;
  name: string;
  description: string;
  category: string;
  connectionsCount: number;
  lastUpdated: string;      // ISO
  signals: HubSignal[];
  proposals: HubProposal[];
}
```

---

### 2) Static hub data (`public/data/knowledge-hubs/moc.json`)

```json
{
  "hubId": "moc",
  "name": "MOC",
  "description": "Master Operations Center — highest-signal hub for cost governance decisions and anomaly patterns.",
  "category": "Governance",
  "connectionsCount": 1243,
  "lastUpdated": "2026-05-03T02:04:37Z",
  "signals": [
    {
      "id": "S-001",
      "type": "anomaly",
      "severity": "high",
      "title": "Unexpected EC2 spend spike in us-east-1",
      "value": "+42% vs 7d avg",
      "timestamp": "2026-05-03T01:45:00Z",
      "tags": ["AWS", "EC2", "us-east-1"]
    },
    {
      "id": "S-002",
      "type": "recommendation",
      "severity": "medium",
      "title": "RI coverage below target for production RDS",
      "value": "68% (target 85%)",
      "timestamp": "2026-05-02T23:10:00Z",
      "tags": ["AWS", "RDS", "RI"]
    }
  ],
  "proposals": [
    {
      "id": "P-001",
      "title": "Purchase 1yr No Upfront RDS RI for primary instances",
      "summary": "Reduce run-rate with minimal cash outlay for primary RDS instances.",
      "impact": "-18% monthly run-rate",
      "priority": "high",
      "signalScore": 88,
      "confidence": 0.81,
      "tags": ["AWS", "RDS", "RI", "cost-optimization"],
      "deadline": "2026-05-10T00:00:00Z",
      "actions": [
        { "label": "Review", "handler": undefined },
        { "label": "Approve", "handler": undefined },
        { "label": "Handoff to change management", "href": "/change-requests/new", "type": "internal" }
      ],
      "updatedAt": "2026-05-02T18:00:00Z"
    },
    {
      "id": "P-002",
      "title": "Schedule idle-dev instance shutdown policy",
      "summary": "Automated nightly shutdown for non-prod dev instances with opt-out tagging.",
      "impact": "-7% monthly run-rate",
      "priority": "medium",
      "signalScore": 74,
      "confidence": 0.74,
      "tags": ["AWS", "EC2", "dev", "automation"],
      "deadline": "2026-05-08T00:00:00Z",
      "actions": [
        { "label": "Review", "handler": undefined },
        { "label": "Schedule", "handler": undefined },
        { "label": "Notify owners", "href": "mailto:dev-owners@example.com", "type": "external" }
      ],
      "updatedAt": "2026-05-01T12:00:00Z"
    }
  ]
}
```

---

### 3) Component: `TopHubSignalPanel.tsx` (detailed view)

```tsx
import React, { useEffect, useState } from 'react';
import type { KnowledgeHub, HubSignal, HubProposal } from '../types/hub';
import './TopHubSignalPanel.css';

const SEVERITY_ICON = {
  high: '⚠️',
  medium: '🔶',
  low: '🔹',
  info: 'ℹ️'
} as const;

const PRIORITY_CLASS = {
  critical: 'priority-critical',
  high: 'priority-high',
  medium: 'priority-medium',
  low: 'priority-low'
} as const;

interface Props {
  cdnUrl?: string;
}

export default function TopHubSignalPanel({ cdnUrl = '/data/knowledge-hubs/moc.json' }: Props) {
  const [data, setData] = useState<KnowledgeHub | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(cdnUrl, { cache: 'no-cache' })
      .then((r) => {
        if (!r.ok) throw new Error(`Failed to load top-hub signals: ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [cdnUrl]);

  if (loading) return <div className="top-hub-panel loading">Loading signals…</div>;
  if (error) return <div className="top-hub-panel error">{error}</div>;
  if (!data) return null;

  return (
    <div className="top-hub-panel" aria-label={`${data.name} hub signals and proposals`}>
      <header className="top-hub-header">
        <div>
          <h2>{data.name} <span className="muted">({data.hubId})</span></h2>
          <p className="muted">{data.description}</p>
        </div>
        <time className="muted small">Updated {new Date(data.lastUpdated).toLocaleString()}</
