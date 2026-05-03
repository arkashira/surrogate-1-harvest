# Costinel / quality

## Implementation Plan: Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Files touched**:
- `src/components/dashboard/TopHubSignalPanel.tsx` (new)
- `src/pages/Dashboard.tsx` (import + mount)
- `src/types/knowledge-rag.ts` (types)
- `src/lib/knowledge-rag.ts` (graph client stub)

---

### 1) Types (`src/types/knowledge-rag.ts`)

```ts
// src/types/knowledge-rag.ts
export interface KnowledgeHub {
  id: string;
  label: string;
  type: 'MOC' | 'TAG' | 'TOPIC' | 'DOC';
  connections: number;
  lastUpdated: string;
}

export interface ActionableProposal {
  id: string;
  title: string;
  summary: string;
  hubId: string;
  impact: 'HIGH' | 'MEDIUM' | 'LOW';
  rationale: string;
  nextAction?: string;
}

export interface TopHubPayload {
  topHub: KnowledgeHub | null;
  proposals: ActionableProposal[];
  generatedAt: string;
}
```

---

### 2) Graph client stub (`src/lib/knowledge-rag.ts`)

```ts
// src/lib/knowledge-rag.ts
import { TopHubPayload, KnowledgeHub, ActionableProposal } from '@/types/knowledge-rag';

const API_BASE = '/api/knowledge-rag';

export async function fetchTopHubSignal(): Promise<TopHubPayload> {
  // In production: GET /api/knowledge-rag/top-hub
  // For now: lightweight stub that returns deterministic demo data
  // so UI can be built without backend changes.
  const demo: TopHubPayload = {
    topHub: {
      id: 'MOC-2026-04-27',
      label: 'MOC',
      type: 'MOC',
      connections: 128,
      lastUpdated: '2026-04-27T14:30:00Z',
    },
    proposals: [
      {
        id: 'prop-001',
        title: 'Standardize RI purchases for MOC workloads',
        summary: 'Shift 40% of on-demand MOC spend to 1-year convertible RIs to reduce cost 22%.',
        hubId: 'MOC-2026-04-27',
        impact: 'HIGH',
        rationale: 'High connection count indicates MOC is a primary cost driver; RI coverage is low.',
        nextAction: 'Review RI recommendation report',
      },
      {
        id: 'prop-002',
        title: 'Enable zonal redundancy for MOC storage',
        summary: 'Move cold MOC artifacts to lower-cost storage classes with lifecycle policy.',
        hubId: 'MOC-2026-04-27',
        impact: 'MEDIUM',
        rationale: 'Storage growth correlates with MOC activity; lifecycle rules missing.',
        nextAction: 'Create storage policy proposal',
      },
    ],
    generatedAt: new Date().toISOString(),
  };

  // Real implementation would be:
  // const res = await fetch(`${API_BASE}/top-hub`);
  // if (!res.ok) throw new Error('Failed to fetch top-hub signal');
  // return res.json();

  return demo;
}
```

---

### 3) Panel component (`src/components/dashboard/TopHubSignalPanel.tsx`)

```tsx
// src/components/dashboard/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal } from '@/lib/knowledge-rag';
import { TopHubPayload, KnowledgeHub, ActionableProposal } from '@/types/knowledge-rag';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { AlertCircle, Lightbulb } from 'lucide-react';

const impactColors = {
  HIGH: 'bg-red-100 text-red-800',
  MEDIUM: 'bg-amber-100 text-amber-800',
  LOW: 'bg-blue-100 text-blue-800',
} as const;

export function TopHubSignalPanel() {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubSignal()
      .then((data) => {
        if (mounted) setPayload(data);
      })
      .catch((err) => {
        if (mounted) setError(err.message ?? 'Unknown error');
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) return <TopHubSignalPanelSkeleton />;
  if (error) return <TopHubSignalPanelError error={error} />;
  if (!payload?.topHub) return null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <CardTitle className="text-base font-semibold flex items-center gap-2">
          <Lightbulb className="h-5 w-5 text-amber-500" />
          Top-hub Signal
        </CardTitle>
        <Badge variant="outline" className="text-xs">
          {payload.topHub.type}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-4">
        <TopHubSummary hub={payload.topHub} />
        <ProposalsList proposals={payload.proposals} />
        <p className="text-xs text-muted-foreground">
          Generated {new Date(payload.generatedAt).toLocaleString()}
        </p>
      </CardContent>
    </Card>
  );
}

function TopHubSummary({ hub }: { hub: KnowledgeHub }) {
  return (
    <div className="rounded-lg border bg-muted/50 p-3">
      <div className="flex items-center justify-between">
        <div>
          <p className="font-semibold text-lg">{hub.label}</p>
          <p className="text-sm text-muted-foreground">
            {hub.connections} connections
          </p>
        </div>
        <Badge variant="secondary">{hub.type}</Badge>
      </div>
    </div>
  );
}

function ProposalsList({ proposals }: { proposals: ActionableProposal[] }) {
  if (!proposals.length) {
    return <p className="text-sm text-muted-foreground">No proposals available.</p>;
  }

  return (
    <div className="space-y-3">
      {proposals.map((p) => (
        <div
          key={p.id}
          className="rounded-md border p-3 text-sm hover:bg-muted/30 transition-colors"
        >
          <div className="flex items-start justify-between gap-2">
            <p className="font-medium">{p.title}</p>
            <Badge className={impactColors[p.impact]}>{p.impact}</Badge>
          </div>
          <p className="text-muted-foreground mt-1">{p.summary}</p>
          <p className="text-xs text-muted-foreground/80 mt-2">{p.rationale}</p>
          {p.nextAction && (
            <p className="text-xs font-medium mt-2 text-primary">
              Next: {p.nextAction}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

function TopHubSignalPanelSkeleton() {
  return (
    <Card>
      <CardHeader className="pb-3">
        <Skeleton className="h-5 w-32" />
      </CardHeader>
      <CardContent className="space-y-3">
        <Skeleton className="h-
