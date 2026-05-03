# Costinel / frontend

## Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Risk**: None (no backend changes; resilient to missing graph data).

### 1) Strategy
- Add a new dashboard widget: **Top-hub Signal Panel**.
- Static-first: embed a curated “MOC” hub summary (from #knowledge-rag #hub) as default content.
- Optional runtime fetch: attempt to GET `/api/knowledge-graph/top-hub` (if available) and merge; fallback to static.
- Design: card with hub title, short insight, proposals list, and “View in Graph” action.

### 2) File Changes
- `src/components/dashboard/TopHubSignalPanel.tsx` — new component.
- `src/pages/Dashboard.tsx` — import and mount panel in the main grid.
- `src/types/knowledgeGraph.ts` — add lightweight types.
- `src/constants/topHub.ts` — static fallback (MOC hub insight).

### 3) Implementation Steps (≤2h)
1. Create types and static content (10 min).
2. Build the panel component with fetch + fallback (30 min).
3. Wire into Dashboard layout (10 min).
4. Add tests/snapshots (optional, 20 min) and polish styles (20 min).
5. Verify build and run smoke test (20 min).

---

### Code Snippets

#### `src/types/knowledgeGraph.ts`
```ts
export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  action?: string; // e.g., "create RI", "rightsize instance"
}

export interface TopHub {
  hubId: string;
  title: string;
  shortInsight: string;
  proposals: Proposal[];
  graphUrl?: string;
  updatedAt?: string;
}
```

#### `src/constants/topHub.ts`
```ts
import { TopHub } from '../types/knowledgeGraph';

export const FALLBACK_TOP_HUB: TopHub = {
  hubId: 'MOC',
  title: 'MOC (Mission Operations Center)',
  shortInsight:
    'Most-connected hub across cost governance workflows. Central to anomaly detection, approval routing, and policy enforcement. Signals here propagate to 12+ downstream services.',
  proposals: [
    {
      id: 'prop-001',
      title: 'Standardize tagging enforcement at MOC egress',
      summary: 'Apply mandatory cost-center tags on resources created via MOC pipelines to improve chargeback accuracy.',
      impact: 'high',
      action: 'enable tag-policy',
    },
    {
      id: 'prop-002',
      title: 'Right-size over-provisioned runner fleet',
      summary: 'MOC runners show 62% avg CPU idle. Recommend moving to smaller instance classes with auto-scale.',
      impact: 'high',
      action: 'rightsize-runners',
    },
    {
      id: 'prop-003',
      title: 'Introduce RI coverage for steady-state MOC services',
      summary: 'Baseline MOC services are stable 24/7. 1-year RIs projected to save ~38% on these workloads.',
      impact: 'medium',
      action: 'purchase-ri',
    },
  ],
  graphUrl: '/graph?hub=MOC',
  updatedAt: '2026-04-27',
};
```

#### `src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { TopHub, Proposal } from '../../types/knowledgeGraph';
import { FALLBACK_TOP_HUB } from '../../constants/topHub';
import './TopHubSignalPanel.css';

const fetchTopHub = async (): Promise<TopHub | null> => {
  try {
    const res = await fetch('/api/knowledge-graph/top-hub', {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
    });
    if (!res.ok) return null;
    const data = (await res.json()) as TopHub;
    return data;
  } catch {
    return null;
  }
};

const impactColor = (impact: Proposal['impact']): string => {
  switch (impact) {
    case 'high':
      return 'var(--impact-high, #ef4444)';
    case 'medium':
      return 'var(--impact-medium, #f59e0b)';
    case 'low':
      return 'var(--impact-low, #10b981)';
  }
};

const ProposalItem: React.FC<{ proposal: Proposal }> = ({ proposal }) => (
  <li className="top-hub-proposal">
    <div className="top-hub-proposal-header">
      <strong>{proposal.title}</strong>
      <span
        className="top-hub-impact-badge"
        style={{ backgroundColor: impactColor(proposal.impact) }}
      >
        {proposal.impact}
      </span>
    </div>
    <p className="top-hub-proposal-summary">{proposal.summary}</p>
    {proposal.action && (
      <small className="top-hub-action">Action: {proposal.action}</small>
    )}
  </li>
);

const TopHubSignalPanel: React.FC = () => {
  const [hub, setHub] = useState<TopHub>(FALLBACK_TOP_HUB);

  useEffect(() => {
    let mounted = true;
    fetchTopHub().then((fetched) => {
      if (mounted && fetched) setHub(fetched);
    });
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <div className="top-hub-panel" data-testid="top-hub-panel">
      <div className="top-hub-panel-header">
        <h3 className="top-hub-title">{hub.title}</h3>
        <small className="top-hub-sub">hub: {hub.hubId}</small>
      </div>
      <p className="top-hub-insight">{hub.shortInsight}</p>

      <div className="top-hub-proposals">
        <h4>Actionable Proposals</h4>
        <ul>
          {hub.proposals.map((p) => (
            <ProposalItem key={p.id} proposal={p} />
          ))}
        </ul>
      </div>

      {hub.graphUrl && (
        <footer className="top-hub-footer">
          <a href={hub.graphUrl} className="top-hub-cta">
            View in Knowledge Graph →
          </a>
          {hub.updatedAt && (
            <small className="top-hub-updated">Updated {hub.updatedAt}</small>
          )}
        </footer>
      )}
    </div>
  );
};

export default TopHubSignalPanel;
```

#### `src/components/dashboard/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.top-hub-panel-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.top-hub-title {
  margin: 0;
  font-size: 1.125rem;
}

.top-hub-sub {
  color: #6b7280;
}

.top-hub-insight {
  margin: 0;
  color: #374151;
  font-size: 0.9375rem;
  line-height: 1.4;
}

.top-hub-proposals h4 {
  margin: 4px 0 8px;
  font-size: 0.875rem;
  color: #111827;
}

.top-hub-proposal {
  list-style: none;
 
