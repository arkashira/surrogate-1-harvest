# Costinel / frontend

**Final Implementation Plan — Costinel “Top-Hub Signal” Card (frontend-only, production-ready)**

**Scope & Constraints**  
- Pure frontend, zero backend changes.  
- Timebox ≤2h.  
- Pattern: Sense + Signal (read-only).  
- Must be immediately mergeable and runnable in dev/prod.

---

### 1) Highest-value improvement (why this wins)
- **Immediate user value**: surfaces the most-connected hub and 3 contextual signals without waiting for backend work.  
- **Zero infra**: uses a committed or CI-generated JSON snapshot in `public/knowledge-rag/`.  
- **Observable & trustworthy**: manual + auto-refresh, last-updated timestamp, and clear error states.  
- **Non-breaking**: fits existing Tailwind + React stack; no layout thrashing.

---

### 2) File layout (additions/modifications)
```
/opt/axentx/Costinel/
├── public/
│   └── knowledge-rag/
│       └── graph-latest.json
├── src/
│   ├── components/
│   │   └── TopHubSignalCard/
│   │       ├── TopHubSignalCard.tsx
│   │       └── TopHubSignalCard.module.css
│   ├── hooks/
│   │   └── useKnowledgeRagGraph.ts
│   └── types/
│       └── knowledge-rag.d.ts
```

---

### 3) Concrete implementation steps (≤2h)

1. **Add snapshot** (`public/knowledge-rag/graph-latest.json`) — use the sample below.  
2. **Create types** (`src/types/knowledge-rag.d.ts`).  
3. **Create hook** (`src/hooks/useKnowledgeRagGraph.ts`) — fetch + compute top hub + signals.  
4. **Create component** (`src/components/TopHubSignalCard/TopHubSignalCard.tsx`) — render card, refresh, timestamp, errors.  
5. **Wire into dashboard** — import and place in Costinel analytics or recommendations section.  
6. **Polish** — theme tokens, responsive, accessible (aria-live), keyboard-friendly refresh.

---

### 4) Code (merged best parts, corrected + actionable)

#### public/knowledge-rag/graph-latest.json
```json
{
  "generatedAt": "2026-05-03T04:00:00Z",
  "nodes": [
    { "id": "MOC", "label": "MOC", "type": "hub" },
    { "id": "RI", "label": "Reserved Instances", "type": "topic" },
    { "id": "SavingsPlans", "label": "Savings Plans", "type": "topic" },
    { "id": "AnomalyDetection", "label": "Anomaly Detection", "type": "topic" },
    { "id": "Forecasting", "label": "Forecasting", "type": "topic" },
    { "id": "Governance", "label": "Governance", "type": "topic" }
  ],
  "edges": [
    { "source": "MOC", "target": "RI" },
    { "source": "MOC", "target": "SavingsPlans" },
    { "source": "MOC", "target": "AnomalyDetection" },
    { "source": "MOC", "target": "Forecasting" },
    { "source": "MOC", "target": "Governance" },
    { "source": "RI", "target": "SavingsPlans" }
  ]
}
```

#### src/types/knowledge-rag.d.ts
```ts
export interface KnowledgeRagGraph {
  generatedAt: string;
  nodes: Array<{ id: string; label: string; type: string }>;
  edges: Array<{ source: string; target: string }>;
}

export interface TopHubSignal {
  hubId: string;
  hubLabel: string;
  degree: number;
  signals: Array<{ id: string; label: string; type: string }>;
}
```

#### src/hooks/useKnowledgeRagGraph.ts
```ts
import { useEffect, useState, useCallback } from 'react';
import type { KnowledgeRagGraph, TopHubSignal } from '../types/knowledge-rag';

const GRAPH_URL = '/knowledge-rag/graph-latest.json';

export function useKnowledgeRagGraph(enabled = true, autoRefreshMs?: number) {
  const [graph, setGraph] = useState<KnowledgeRagGraph | null>(null);
  const [topHub, setTopHub] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchedAt, setLastFetchedAt] = useState<Date | null>(null);

  const computeTopHub = useCallback((g: KnowledgeRagGraph): TopHubSignal | null => {
    const degree: Record<string, number> = {};
    const nodeMap = new Map(g.nodes.map((n) => [n.id, n]));

    g.edges.forEach((e) => {
      degree[e.source] = (degree[e.source] || 0) + 1;
      degree[e.target] = (degree[e.target] || 0) + 1;
    });

    const ids = Object.keys(degree);
    if (ids.length === 0) return null;

    const hubId = ids.reduce((a, b) => (degree[a] > degree[b] ? a : b));
    if (!hubId || !nodeMap.has(hubId)) return null;

    const connected = new Set<string>();
    g.edges.forEach((e) => {
      if (e.source === hubId) connected.add(e.target);
      if (e.target === hubId) connected.add(e.source);
    });

    const signals = Array.from(connected)
      .map((id) => nodeMap.get(id))
      .filter(Boolean)
      .filter((n) => n && n.id !== hubId)
      .slice(0, 3) as Array<{ id: string; label: string; type: string }>;

    return {
      hubId,
      hubLabel: nodeMap.get(hubId)!.label,
      degree: degree[hubId],
      signals,
    };
  }, []);

  const fetchGraph = useCallback(
    async (bustCache = false) => {
      if (!enabled) return;
      setLoading(true);
      setError(null);
      try {
        const url = bustCache ? `${GRAPH_URL}?t=${Date.now()}` : GRAPH_URL;
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) throw new Error(`Failed to fetch graph: ${res.status}`);
        const data: KnowledgeRagGraph = await res.json();
        setGraph(data);
        setTopHub(computeTopHub(data));
        setLastFetchedAt(new Date());
      } catch (err: any) {
        setError(err.message || 'Unknown error');
      } finally {
        setLoading(false);
      }
    },
    [enabled, computeTopHub]
  );

  useEffect(() => {
    fetchGraph(false);
  }, [fetchGraph]);

  useEffect(() => {
    if (!autoRefreshMs || autoRefreshMs <= 0) return;
    const id = setInterval(() => fetchGraph(true), autoRefreshMs);
    return () => clearInterval(id);
  }, [autoRefreshMs, fetchGraph]);

  return { graph, topHub, loading, error, lastFetchedAt, refetch: fetchGraph };
}
```

#### src/components/TopHubSignalCard/TopHubSignalCard.tsx
```tsx
import React from 'react';
import { useKnowledgeRagGraph } from '../../hooks/useKnowledgeRagGraph';
import styles from './TopHubSignalCard.module.css';

export const TopHubSignalCard: React.FC<{ autoRefreshMs?: number }> = ({ autoRefreshMs }) => {
  const { topHub, loading, error, lastFetchedAt, refetch } = useKnowledgeRagGraph(true, autoRefreshMs);

  const handleRefresh = () => refetch(true);

  const formatTime = (d: Date | null) => (d ? d.toLocaleString() : '-');

  return (
    <div className
