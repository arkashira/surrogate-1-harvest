# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (≤2h, frontend-only)

**Scope & constraints**  
- Pure frontend, zero backend, read-only (Sense + Signal).  
- ≤2h timebox.  
- No new APIs, no infra, no auth.  
- Must render even when graph data is unavailable (graceful fallback).

---

### 1) Highest-value deliverable
Embed a lightweight card into the Costinel dashboard that:
- Reads a static knowledge-rag graph export (JSON).  
- Identifies the node with highest degree (most connections) as the top hub.  
- Renders the hub name + exactly 3 contextual signals (neighbor summaries with optional links and tags).  
- Uses existing design tokens and fits the current dashboard grid.

**Why this ships fast**: static JSON + frontend component only. Delivers immediate “Sense + Signal” value and proves the knowledge-rag → dashboard path.

---

### 2) File changes
- `/opt/axentx/Costinel/src/components/TopHubSignalCard.vue` (new)  
- `/opt/axentx/Costinel/src/lib/graphClient.ts` (new)  
- `/opt/axentx/Costinel/src/views/Dashboard.vue` — mount card in top-right of cost overview panel  
- `/opt/axentx/Costinel/public/knowledge-rag-graph.json` (static export; include minimal fallback if absent)

---

### 3) Data contract (static JSON)
Expected at `/knowledge-rag-graph.json` (public, CDN-friendly):

```json
{
  "nodes": [
    { "id": "MOC", "label": "Mission Operating Center", "updatedAt": "2025-01-01T12:00:00Z" },
    { "id": "S-001", "label": "Q2 cloud run-rate variance +12%", "updatedAt": "2025-01-02T08:00:00Z" },
    { "id": "S-002", "label": "Top savings opportunity: unused EBS snapshots", "updatedAt": "2025-01-02T09:00:00Z" },
    { "id": "S-003", "label": "Governance policy drift detected", "updatedAt": "2025-01-02T10:00:00Z" }
  ],
  "edges": [
    { "source": "MOC", "target": "S-001", "weight": 8 },
    { "source": "MOC", "target": "S-002", "weight": 6 },
    { "source": "MOC", "target": "S-003", "weight": 5 }
  ]
}
```

If missing, the card uses an embedded fallback (see below).

---

### 4) Implementation details

#### graphClient.ts — zero-backend hub + signals fetcher
```ts
// src/lib/graphClient.ts
export interface HubSignal {
  id: string;
  title: string;
  snippet?: string;
  href?: string;
  tags?: string[];
  updatedAt: string;
}

export interface TopHub {
  hubId: string;
  label: string;
  connections: number;
  signals: HubSignal[];
  updatedAt: string;
}

const GRAPH_URL = '/knowledge-rag-graph.json';

const FALLBACK: TopHub = {
  hubId: 'MOC',
  label: 'Mission Operating Center',
  connections: 142,
  updatedAt: new Date().toISOString(),
  signals: [
    {
      id: 'S-001',
      title: 'Q2 cloud run-rate variance +12%',
      snippet: 'AWS compute spend trending above forecast; review RI coverage.',
      href: '/costs/forecast',
      tags: ['aws', 'ri', 'variance'],
      updatedAt: new Date().toISOString()
    },
    {
      id: 'S-002',
      title: 'Top savings opportunity: unused EBS snapshots',
      snippet: '340 GB of unattached snapshots across prod accounts.',
      href: '/recommendations/storage',
      tags: ['ebs', 'snapshots', 'savings'],
      updatedAt: new Date().toISOString()
    },
    {
      id: 'S-003',
      title: 'Governance policy drift detected',
      snippet: 'Two accounts missing mandatory tag enforcement.',
      href: '/governance/tags',
      tags: ['governance', 'tags', 'audit'],
      updatedAt: new Date().toISOString()
    }
  ]
};

export interface GraphData {
  nodes: Array<{ id: string; label: string; updatedAt?: string }>;
  edges: Array<{ source: string; target: string; weight?: number }>;
}

export async function fetchTopHub(): Promise<TopHub> {
  try {
    const res = await fetch(GRAPH_URL, { cache: 'no-store' });
    if (!res.ok) throw new Error('Graph fetch failed');
    const graph = (await res.json()) as GraphData;

    // compute degree per node
    const degree: Record<string, number> = {};
    const nodeMap = new Map<string, { label: string; updatedAt: string }>();
    for (const n of graph.nodes) {
      degree[n.id] = 0;
      nodeMap.set(n.id, { label: n.label || n.id, updatedAt: n.updatedAt || new Date().toISOString() });
    }
    for (const e of graph.edges) {
      degree[e.source] = (degree[e.source] || 0) + 1;
      // count both directions if undirected; here treat as directed out-degree for simplicity
      degree[e.target] = (degree[e.target] || 0) + 1;
    }

    const topNodeId = Object.entries(degree).sort((a, b) => b[1] - a[1])[0]?.[0];
    if (!topNodeId) throw new Error('No nodes found');

    // collect neighbor signals (targets and sources linked to top node)
    const neighborIds = new Set<string>();
    for (const e of graph.edges) {
      if (e.source === topNodeId) neighborIds.add(e.target);
      if (e.target === topNodeId) neighborIds.add(e.source);
    }

    const neighbors = Array.from(neighborIds)
      .map(id => {
        const n = nodeMap.get(id);
        return n ? { ...n, id } : null;
      })
      .filter(Boolean) as Array<{ id: string; label: string; updatedAt: string }>;

    // sort by presence/weight? edges with weight can be used; here stable by id
    const signals = neighbors.slice(0, 3).map(n => ({
      id: n.id,
      title: n.label,
      snippet: '',
      href: undefined,
      tags: [],
      updatedAt: n.updatedAt
    }));

    const topNode = nodeMap.get(topNodeId)!;
    return {
      hubId: topNodeId,
      label: topNode.label,
      connections: degree[topNodeId],
      signals,
      updatedAt: topNode.updatedAt
    };
  } catch (err) {
    console.warn('[graphClient] using fallback top hub', err);
    return { ...FALLBACK, updatedAt: new Date().toISOString() };
  }
}
```

---

#### TopHubSignalCard.vue — read-only card UI
```vue
<!-- src/components/TopHubSignalCard.vue -->
<template>
  <section class="top-hub-card" aria-label="Top hub signal">
    <header class="card-header">
      <h3 class="hub-title">{{ hub.label }}</h3>
      <span class="hub-meta">{{ hub.connections }} connections</span>
    </header>

    <ul class="signals-list" role="list">
      <li v-for="s in hub.signals" :key="s.id" class="signal-item">
        <div class="signal-title">
          <a v-if="s.href" :href="s.href" class="signal-link">{{ s.title }}</a>
          <span v-else class="signal-text">{{ s.title }}</span>
        </div>
        <
