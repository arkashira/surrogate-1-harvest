# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Constraints**: Read-only, no execute actions; uses existing `/api/knowledge-rag/top-hub` endpoint (or graph fallback). ETA: <2h.

---

### 1) File changes

- `src/components/cards/TopHubSignalCard.tsx` (new) — standalone card (React).
- `src/lib/knowledge-rag.ts` (new) — thin client for top-hub + signals.
- `src/pages/dashboard/Dashboard.tsx` (or equivalent) — import and mount card in the visibility/governance section.
- `public/mock/top-hub.json` — local mock for dev/offline.

---

### 2) API utility (`src/lib/knowledge-rag.ts`)

```ts
// src/lib/knowledge-rag.ts
export interface HubSignal {
  id: string;
  label: string;
  category?: string;
  score?: number;
  snippet?: string;
}

export interface TopHubPayload {
  hub: {
    id: string;
    label: string;
    description?: string;
    degree?: number;
  };
  signals: HubSignal[];
}

const API_BASE = '/api/knowledge-rag';

export async function fetchTopHub(signalLimit = 3): Promise<TopHubPayload | null> {
  try {
    // Primary endpoint: dedicated top-hub
    const res = await fetch(`${API_BASE}/top-hub?limit=${signalLimit}`, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
    });

    if (res.ok) {
      const data = await res.json();
      return normalizeTopHub(data, signalLimit);
    }

    // Fallback: use graph endpoint and compute top degree node client-side
    const graphRes = await fetch(`${API_BASE}/graph`, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
    });

    if (graphRes.ok) {
      const graph = await graphRes.json();
      return computeTopHubFromGraph(graph, signalLimit);
    }

    // Final fallback: local mock (dev/offline)
    try {
      const mockRes = await fetch('/mock/top-hub.json');
      if (mockRes.ok) {
        const mock = await mockRes.json();
        return normalizeTopHub(mock, signalLimit);
      }
    } catch {
      // ignore
    }

    return null;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[knowledge-rag] failed to fetch top hub', err);
    return null;
  }
}

function normalizeTopHub(data: any, limit: number): TopHubPayload | null {
  if (!data || (!data.hub && !data.node)) return null;
  const hub = data.hub || data.node;
  const signals = Array.isArray(data.signals || data.related || [])
    ? (data.signals || data.related || []).slice(0, limit)
    : [];
  return {
    hub: {
      id: hub.id || hub.label || 'unknown',
      label: hub.label || hub.name || 'Unknown hub',
      description: hub.description || hub.snippet || '',
      degree: hub.degree || hub.score || undefined,
    },
    signals: signals.map((s: any) => ({
      id: s.id || s.label || crypto.randomUUID(),
      label: s.label || s.name || 'Signal',
      category: s.category,
      score: s.score,
      snippet: s.snippet,
    })),
  };
}

function computeTopHubFromGraph(graph: any, limit: number): TopHubPayload | null {
  // Expected shape: { nodes: [...], edges: [...] } or { vertices: [...], links: [...] }
  const nodes = graph.nodes || graph.vertices || [];
  const edges = graph.edges || graph.links || [];
  if (!Array.isArray(nodes) || !Array.isArray(edges) || nodes.length === 0) return null;

  const degree: Record<string, number> = {};
  for (const n of nodes) {
    const id = n.id || n.label;
    if (id) degree[id] = 0;
  }
  for (const e of edges) {
    const src = e.source || e.from || e[0];
    const tgt = e.target || e.to || e[1];
    if (src && degree[src] !== undefined) degree[src] = (degree[src] || 0) + 1;
    if (tgt && degree[tgt] !== undefined) degree[tgt] = (degree[tgt] || 0) + 1;
  }

  const topId = Object.entries(degree).sort((a, b) => b[1] - a[1])[0]?.[0];
  if (!topId) return null;

  const topNode = nodes.find((n) => (n.id || n.label) === topId) || { label: topId };
  const relatedSignals = edges
    .filter((e) => {
      const src = e.source || e.from || e[0];
      const tgt = e.target || e.to || e[1];
      return src === topId || tgt === topId;
    })
    .map((e) => {
      const other = (e.source || e.from || e[0]) === topId ? (e.target || e.to || e[1]) : (e.source || e.from || e[0]);
      return {
        id: other,
        label: other,
        category: e.category || e.type,
        score: e.weight || e.score,
        snippet: e.snippet,
      };
    })
    .slice(0, limit);

  return {
    hub: {
      id: topNode.id || topNode.label || topId,
      label: topNode.label || topNode.name || topId,
      description: topNode.description || topNode.snippet || '',
      degree: degree[topId],
    },
    signals: relatedSignals,
  };
}
```

---

### 3) Card component (`src/components/cards/TopHubSignalCard.tsx`)

```tsx
// src/components/cards/TopHubSignalCard.tsx
import { useEffect, useState } from 'react';
import { fetchTopHub, type TopHubPayload } from '../../lib/knowledge-rag';
import './TopHubSignalCard.css';

const CACHE_KEY = 'costinel:top-hub:cached';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5m

export default function TopHubSignalCard() {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const cached = sessionStorage.getItem(CACHE_KEY);
    if (cached) {
      try {
        const { data, ts } = JSON.parse(cached);
        if (Date.now() - ts < CACHE_TTL_MS) {
          setPayload(data);
          setLoading(false);
          return;
        }
      } catch {
        sessionStorage.removeItem(CACHE_KEY);
      }
    }

    let mounted = true;
    setLoading(true);
    fetchTopHub(3)
      .then((data) => {
        if (!mounted) return;
        setPayload(data);
        if (data) {
          sessionStorage.setItem(CACHE_KEY, JSON.stringify({ data, ts: Date.now() }));
        }
      })
      .finally(() => mounted && setLoading(false));

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-card card-skeleton" role="status" aria-busy="true">
        <div className="skeleton-line" style={{ width: '60%' }} />
        <div className="skeleton-line" style={{ width: '90%' }} />
        <div className="signals-row">
          <div className="skeleton-chip" />
          <div className="skeleton-chip" />
          <
