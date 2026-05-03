# Costinel / backend

## Implementation Plan: Costinel “Top-Hub Signal” Card (Backend-Ready Signal Endpoint)

**Scope**: Add a lightweight backend endpoint (`GET /api/signals/top-hub`) that surfaces the most-connected hub + related docs for the Costinel dashboard. Uses existing knowledge-rag graph; no infra changes; read-only; ≤2h.

**Why this is highest-value**: Gives the frontend an authoritative signal source (instead of static card), enables future automation (alerts, proposals), and aligns with pattern “review top-hub before planning”.

---

### 1) File changes (backend)

- `src/server/routes/signals.ts` — new route
- `src/server/services/knowledgeRag.ts` — thin wrapper around graph queries
- `src/types/signals.ts` — type definitions
- `src/server/app.ts` — mount route

### 2) Implementation details

- Query graph for node with highest degree (or `pagerank` if available).
- Fetch 1-hop related docs (neighbors + edge metadata).
- Return `{ hub, related, generatedAt }`.
- Cache in-memory for 60s to avoid heavy graph scans on every dashboard load.
- No auth changes (read-only); errors return 500 with safe fallback.

---

### Code snippets

#### `src/types/signals.ts`
```ts
export interface RelatedDoc {
  id: string;
  title?: string;
  source?: string;
  score?: number;
  relationType?: string;
}

export interface TopHubSignal {
  hub: {
    id: string;
    label: string;
    type?: string;
    degree?: number;
  };
  related: RelatedDoc[];
  generatedAt: string; // ISO
}
```

#### `src/server/services/knowledgeRag.ts`
```ts
import { Graph } from '../lib/graph'; // assume existing graph abstraction

const CACHE_TTL_MS = 60_000;
let cached: { signal: any; expiresAt: number } | null = null;

export async function getTopHubSignal(): Promise<any> {
  const now = Date.now();
  if (cached && cached.expiresAt > now) return cached.signal;

  // Example: graph API exists; adapt to your actual store
  const graph = await Graph.getInstance();

  // Find top hub by degree (fallback to first node if none)
  const topNode = graph.nodes().reduce((best, node) => {
    const deg = graph.degree(node);
    return deg > best.degree ? { node, degree: deg } : best;
  }, { node: graph.nodes()[0] || null, degree: -1 });

  if (!topNode.node) {
    return { hub: null, related: [], generatedAt: new Date().toISOString() };
  }

  const hub = {
    id: topNode.node,
    label: graph.getNodeLabel(topNode.node) || topNode.node,
    type: graph.getNodeType(topNode.node),
    degree: topNode.degree,
  };

  // 1-hop related docs (neighbors + edge metadata)
  const neighbors = graph.neighbors(topNode.node);
  const related = neighbors.slice(0, 10).map((nid) => ({
    id: nid,
    title: graph.getNodeLabel(nid) || nid,
    source: graph.getNodeSource(nid),
    relationType: graph.getEdgeType(topNode.node, nid),
    score: graph.getEdgeWeight(topNode.node, nid),
  }));

  const signal = { hub, related, generatedAt: new Date().toISOString() };
  cached = { signal, expiresAt: now + CACHE_TTL_MS };
  return signal;
}
```

#### `src/server/routes/signals.ts`
```ts
import express from 'express';
import { getTopHubSignal } from '../services/knowledgeRag';

const router = express.Router();

router.get('/top-hub', async (req, res) => {
  try {
    const signal = await getTopHubSignal();
    res.json(signal);
  } catch (err) {
    console.error('[signals] top-hub error', err);
    res.status(500).json({ hub: null, related: [], generatedAt: new Date().toISOString() });
  }
});

export default router;
```

#### `src/server/app.ts` (mount)
```ts
import signalsRouter from './routes/signals';
app.use('/api/signals', signalsRouter);
```

---

### 3) Validation & rollout

- Start dev server and hit `GET /api/signals/top-hub` — expect JSON with `hub` and `related`.
- If graph not populated, seed with existing RAG export or return empty shape (safe fallback).
- Frontend can consume `/api/signals/top-hub` and render dynamic card (future PR).
- No breaking changes; cache prevents load spikes.

**Estimated effort**: ~90 minutes (code + test + PR).
