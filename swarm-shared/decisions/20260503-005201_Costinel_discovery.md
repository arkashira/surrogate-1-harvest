# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (≤2h, frontend-only)

**Goal**  
Ship a read-only, frontend-only card that:
- Detects the highest-degree hub from the knowledge-rag graph (e.g., `"MOC"`).
- Shows 3 contextual signals (short insights/docs linked to that hub).
- Fails gracefully when graph data is missing or malformed.
- Fits existing Costinel design system and emits no runtime exceptions.

**Scope & Constraints**  
- Pure frontend — no backend, no new APIs, no auth, no infra.  
- Read-only — Sense + Signal only (no execution).  
- Timebox — ≤2h.  
- Resilience — graceful fallback UI when graph unavailable.

---

### 1) File changes (new/modified)

- `src/data/knowledgeGraph.ts`  
  Types + lightweight helpers to compute top hub and contextual signals.

- `src/components/cards/TopHubSignalCard.vue` (or `.tsx` if React)  
  Self-contained card:
  - Accepts optional `graph` prop; defaults to mock data.
  - Picks hub with max degree.
  - Shows 3 contextual signals.
  - Shows timestamp and graceful fallback UI if no data.

- `src/pages/Dashboard.vue` (or equivalent)  
  Import and mount the card; wire a demo payload for now.

---

### 2) Code snippets

#### `src/data/knowledgeGraph.ts`
```ts
export interface GraphNode {
  id: string;
  label: string;
  type: 'hub' | 'doc' | 'topic';
}

export interface GraphEdge {
  source: string;
  target: string;
}

export interface KnowledgeGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// Lightweight mock — replace later with real graph payload if available
export const mockKnowledgeGraph: KnowledgeGraph = {
  nodes: [
    { id: 'MOC', label: 'MOC', type: 'hub' },
    { id: 'doc-cost-forecast', label: 'Cost Forecast Methods', type: 'doc' },
    { id: 'doc-ri-coverage', label: 'RI Coverage Analysis', type: 'doc' },
    { id: 'doc-budget-alerts', label: 'Budget Alert Patterns', type: 'doc' },
    { id: 'topic-forecasting', label: 'Forecasting', type: 'topic' },
  ],
  edges: [
    { source: 'MOC', target: 'doc-cost-forecast' },
    { source: 'MOC', target: 'doc-ri-coverage' },
    { source: 'MOC', target: 'doc-budget-alerts' },
    { source: 'MOC', target: 'topic-forecasting' },
    { source: 'doc-cost-forecast', target: 'topic-forecasting' },
  ],
};

export function getTopHub(graph: KnowledgeGraph): { hub: GraphNode; degree: number } | null {
  const degree: Record<string, number> = {};
  for (const node of graph.nodes) {
    degree[node.id] = 0;
  }
  for (const edge of graph.edges) {
    degree[edge.source] = (degree[edge.source] || 0) + 1;
    degree[edge.target] = (degree[edge.target] || 0) + 1;
  }

  let topId: string | null = null;
  let topDegree = -1;
  for (const id of Object.keys(degree)) {
    if (degree[id] > topDegree) {
      topDegree = degree[id];
      topId = id;
    }
  }

  if (!topId) return null;
  const hub = graph.nodes.find((n) => n.id === topId);
  return hub ? { hub, degree: topDegree } : null;
}

export function getContextualSignatures(graph: KnowledgeGraph, hubId: string, limit = 3): GraphNode[] {
  const neighbors = graph.edges
    .filter((e) => e.source === hubId || e.target === hubId)
    .map((e) => (e.source === hubId ? e.target : e.source));
  return graph.nodes.filter((n) => neighbors.includes(n.id) && n.id !== hubId).slice(0, limit);
}
```

#### `src/components/cards/TopHubSignalCard.vue`
```vue
<template>
  <section class="costinel-top-hub-card" aria-label="Top hub signal">
    <!-- Header -->
    <header class="card-header">
      <h3 class="card-title">Top hub signal</h3>
      <span v-if="updatedAt" class="card-meta">Updated {{ updatedAt }}</span>
    </header>

    <!-- Fallback -->
    <div v-if="!top" class="empty-state">
      <span class="empty-icon">🔍</span>
      <p class="empty-text">Insights unavailable</p>
    </div>

    <!-- Content -->
    <template v-else>
      <div class="hub-summary">
        <div class="hub-icon" aria-hidden="true">🌐</div>
        <div class="hub-info">
          <strong class="hub-name">{{ top.hub.label }}</strong>
          <span class="hub-degree">{{ top.degree }} connections</span>
        </div>
        <span class="status-badge">Signal</span>
      </div>

      <ul class="signals-list" aria-label="Contextual signals">
        <li v-for="s in signals" :key="s.id" class="signal-item">
          <span class="signal-bullet" aria-hidden="true">•</span>
          <span class="signal-label">{{ s.label }}</span>
        </li>
        <li v-if="signals.length === 0" class="signal-item empty-signal">
          No contextual signals found.
        </li>
      </ul>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { KnowledgeGraph, mockKnowledgeGraph, getTopHub, getContextualSignatures } from '../../data/knowledgeGraph';

const props = defineProps<{
  graph?: KnowledgeGraph;
}>();

const graph = computed(() => props.graph || mockKnowledgeGraph);
const top = computed(() => getTopHub(graph.value));
const signals = computed(() => (top.value ? getContextualSignatures(graph.value, top.value.hub.id, 3) : []));
const updatedAt = computed(() => new Date().toLocaleTimeString());
</script>

<style scoped>
.costinel-top-hub-card {
  border: 1px solid #e5e7eb;
  border-radius: 0.5rem;
  background: #fff;
  padding: 1rem;
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.75rem;
}

.card-title {
  font-size: 0.875rem;
  font-weight: 600;
  color: #111827;
  margin: 0;
}

.card-meta {
  font-size: 0.75rem;
  color: #6b7280;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 1rem;
  color: #9ca3af;
}

.empty-icon {
  font-size: 1.25rem;
  margin-bottom: 0.25rem;
}

.empty-text {
  font-size: 0.875rem;
  margin: 0;
}

.hub-summary {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.hub-icon {
  width: 2rem;
  height: 2rem;
  border-radius: 9999px;
  background: #dbeafe;
  color: #2563eb;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font
