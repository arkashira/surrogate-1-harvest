# Costinel / quality

### Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and the top actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind (resilient to missing backend; uses typed adapter + local fixture)

---

### 1) Design (single component)
- Place in dashboard sidebar or top bar as compact card.
- Show:
  - Most-connected hub name + type + short description
  - Top 3 actionable proposals (title + 1-line rationale) with impact badges
  - “Generated Xm ago” timestamp
- States: loading / data / empty / error (graceful)

---

### 2) Data contract (TypeScript)
```ts
// src/types/knowledge-graph.ts
export interface HubInsight {
  hubId: string;
  hubName: string;
  hubType: 'MOC' | 'TAG' | 'CENTER' | string;
  description: string;
  connectionCount: number;
}

export interface Proposal {
  id: string;
  title: string;
  rationale: string;
  impact: 'high' | 'medium' | 'low';
  href?: string;
}

export interface TopHubSignal {
  hub: HubInsight;
  proposals: Proposal[];
  generatedAt: string; // ISO
}
```

---

### 3) Adapter layer (decouple UI from source)
```ts
// src/lib/knowledge-graph/adapter.ts
import { TopHubSignal } from '../types/knowledge-graph';

const ENDPOINT = '/api/knowledge-graph/top-hub';

export async function fetchTopHubSignal(): Promise<TopHubSignal | null> {
  try {
    const res = await fetch(ENDPOINT, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = await res.json();
    if (json?.hub?.hubId && Array.isArray(json.proposals)) return json as TopHubSignal;
    return null;
  } catch {
    // Fallback fixture (keeps UI working without backend)
    return {
      hub: {
        hubId: 'MOC',
        hubName: 'Mission Operations Center',
        hubType: 'MOC',
        description: 'Central coordination for cloud operations and incident response.',
        connectionCount: 42,
      },
      proposals: [
        {
          id: 'p-1',
          title: 'Standardize tagging for prod-critical workloads',
          rationale: 'Improves cost attribution and anomaly detection for top hubs.',
          impact: 'high',
        },
        {
          id: 'p-2',
          title: 'Add budget guardrails for MOC-linked accounts',
          rationale: 'Prevents surprise spikes tied to mission-critical services.',
          impact: 'medium',
        },
        {
          id: 'p-3',
          title: 'Schedule weekly RI coverage review for MOC services',
          rationale: 'High connection count indicates stable workloads suitable for RIs.',
          impact: 'medium',
        },
      ],
      generatedAt: new Date().toISOString(),
    };
  }
}
```

---

### 4) Component (React + Tailwind)
```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal } from '../lib/knowledge-graph/adapter';
import type { TopHubSignal } from '../types/knowledge-graph';

const impactColor = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-slate-100 text-slate-800',
} as const;

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubSignal()
      .then((res) => {
        if (mounted) {
          setData(res);
          setError(!res);
        }
      })
      .catch(() => {
        if (mounted) setError(true);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-slate-200" />
        <div className="mt-3 h-4 w-24 animate-pulse rounded bg-slate-100" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm text-sm text-slate-500">
        Signal unavailable
      </div>
    );
  }

  const minutesAgo = Math.floor((Date.now() - new Date(data.generatedAt).getTime()) / 60000);

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">{data.hub.hubName}</h3>
          <p className="text-xs text-slate-500">{data.hub.hubType} — {data.hub.description}</p>
        </div>
        <span className="whitespace-nowrap text-xs text-slate-400">{minutesAgo}m ago</span>
      </div>

      <ul className="mt-3 space-y-2">
        {data.proposals.map((p) => (
          <li key={p.id} className="flex gap-2 text-sm">
            <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${impactColor[p.impact]}`}>
              {p.impact}
            </span>
            <div>
              <p className="font-medium text-slate-900">{p.title}</p>
              <p className="text-slate-600">{p.rationale}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

---

### 5) Add to Costinel dashboard
```tsx
// src/components/CostinelDashboard.tsx
import React from 'react';
import TopHubSignalPanel from './TopHubSignalPanel';

const CostinelDashboard: React.FC = () => {
  return (
    <div className="costinel-dashboard space-y-4">
      {/* Existing dashboard components */}
      <TopHubSignalPanel />
    </div>
  );
};

export default CostinelDashboard;
```

---

### 6) Backend endpoint (FastAPI)
```python
# src/api/knowledge_graph.py
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter()

class HubInsightModel(BaseModel):
    hubId: str
    hubName: str
    hubType: str
    description: str
    connectionCount: int

class ProposalModel(BaseModel):
    id: str
    title: str
    rationale: str
    impact: str
    href: str | None = None

class TopHubResponse(BaseModel):
    hub: HubInsightModel
    proposals: List[ProposalModel]
    generatedAt: str

@router.get("/top-hub", response_model=TopHubResponse)
async def get_top_hub():
    # Replace with real knowledge graph query
    return TopHubResponse(
        hub=HubInsightModel(
            hubId="MOC",
            hubName="Mission Operations Center",
            hubType="MOC",
            description="Central coordination for cloud operations and incident response.",
            connectionCount=42,
        ),
        proposals=[
            Proposal
