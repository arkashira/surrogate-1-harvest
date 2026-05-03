# Costinel / discovery

## Final Synthesized Implementation

**Value**: Production-ready, read-only Vue 3 card that surfaces the highest-connected knowledge hub (e.g., "MOC") with cost-governance signals. Uses CDN-bypass for live data, embedded mock fallback for offline/dev, and is SSR-safe.

**ETA**: <2h (≈90m implementation + 30m polish/tests)

---

### 1. Component + Composable Design (merged best parts)

- **Component**: `src/components/cards/TopHubSignalCard.vue`
  - Props: `hubId?`, `date?`, `useMock?`
  - Uses **composable** `useCdnHubInsights` for all data logic (keeps component clean and SSR-safe).
  - No `window` access at import time; lazy-loads fetch only in composable runtime.

- **Composable**: `src/composables/useCdnHubInsights.ts`
  - Accepts `hubId`, `date`, `useMock`.
  - If `hubId` provided → fetch `/cdn/{date}/hubs/{hubId}.json`.
  - Else → fetch `/cdn/{date}/hubs/index.json`, pick highest `connectionScore`, then fetch that hub.
  - On any failure → return embedded mock payload.
  - Exposes `refresh()`, `exportCsv()`, `loading`, `data`, `error`.

- **CDN path resolution**
  - Production: CDN-bypass URLs (same origin `/cdn/...`) to avoid auth complexity.
  - Optional override via prop/env for alternate origins (e.g., HuggingFace) — but default to same-origin for simplicity and reliability.

---

### 2. File Structure & Assets

```
src/
  components/
    cards/
      TopHubSignalCard.vue
  composables/
    useCdnHubInsights.ts
  views/
    ops/
      Dashboard.vue   (import and place card in top-right "Signals" pane)
public/
  cdn/
    2026-05-03/
      hubs/
        index.json
        MOC.json
```

- `index.json`: minimal list `[{ hubId, title, connectionScore }]`
- `MOC.json`: full payload (schema below)

**Payload schema**
```ts
interface Signal {
  id: string;
  type: string;
  summary: string;
  severity: "low" | "medium" | "high";
}
interface Recommendation {
  id: string;
  action: string;
  impact: string;
}
interface HubPayload {
  hubId: string;
  title: string;
  connectionScore: number;
  signals: Signal[];
  recommendations: Recommendation[];
  updatedAt: string;
}
```

---

### 3. Component Implementation (concise, production-ready)

`TopHubSignalCard.vue`
```vue
<template>
  <section class="top-hub-card" aria-labelledby="hub-title">
    <header class="card-header">
      <h2 id="hub-title" class="hub-title">
        {{ data?.title ?? "Loading..." }}
        <span class="connection-badge" :title="`Connections: ${data?.connectionScore}`">
          {{ data?.connectionScore ?? 0 }}
        </span>
      </h2>
      <div class="card-actions">
        <button
          @click="refresh"
          :disabled="loading"
          class="btn-refresh"
          aria-label="Refresh hub data"
        >
          ↻
        </button>
        <button @click="exportCsv" class="btn-export" aria-label="Export as CSV">
          ⬇ CSV
        </button>
      </div>
    </header>

    <div v-if="loading" class="loading" aria-busy="true">
      Loading signals...
    </div>

    <div v-else-if="data" class="card-body">
      <p class="updated">Updated: {{ formatDate(data.updatedAt) }}</p>

      <section class="signals" aria-label="Top signals">
        <h3>Signals</h3>
        <ul>
          <li v-for="s in data.signals" :key="s.id" class="signal-item">
            <strong>{{ s.type }}</strong>: {{ s.summary }}
            <span class="signal-severity" :class="s.severity">{{ s.severity }}</span>
          </li>
        </ul>
      </section>

      <section class="recommendations" aria-label="Recommendations">
        <h3>Recommendations</h3>
        <ul>
          <li v-for="r in data.recommendations" :key="r.id" class="rec-item">
            {{ r.action }} — <em>{{ r.impact }}</em>
          </li>
        </ul>
      </section>
    </div>

    <div v-else class="empty" aria-live="polite">
      No hub data available.
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useCdnHubInsights } from "@/composables/useCdnHubInsights";

const props = withDefaults(
  defineProps<{
    hubId?: string;
    date?: string; // YYYY-MM-DD
    useMock?: boolean;
  }>(),
  {
    date: () => new Date().toISOString().slice(0, 10),
    useMock: false,
  }
);

const { data, loading, error, refresh, exportCsv } = useCdnHubInsights(
  computed(() => props.hubId),
  computed(() => props.date),
  computed(() => props.useMock)
);

function formatDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

onMounted(() => {
  // initial load handled by composable reaction
});
</script>

<style scoped>
.top-hub-card {
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  padding: 16px;
  background: #fff;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.hub-title {
  font-size: 1.125rem;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.connection-badge {
  font-size: 0.75rem;
  padding: 2px 6px;
  border-radius: 999px;
  background: #e0e7ff;
  color: #3730a3;
}
.card-actions {
  display: flex;
  gap: 8px;
}
.btn-refresh,
.btn-export {
  padding: 4px 8px;
  border: 1px solid #d1d5db;
  border-radius: 4px;
  background: #fff;
  cursor: pointer;
}
.loading {
  color: #6b7280;
  padding: 12px 0;
}
.card-body .updated {
  font-size: 0.875rem;
  color: #6b7280;
  margin-bottom: 12px;
}
.signals ul,
.recommendations ul {
  list-style: none;
  padding: 0;
  margin: 8px 0 0 0;
}
.signal-item,
.rec-item {
  padding: 6px 0;
  border-bottom: 1px solid #f3f4f6;
}
.signal-severity {
  margin-left: 8px;
  font-size: 0.75rem;
  text-transform: capitalize;
}
.signal-severity.high { color: #b91c1c; }
.signal-severity.medium { color: #d97706; }
.signal-severity.low { color: #16a34a; }
</style>
```

---

### 4. Composable Implementation (SSR-safe)

`useCdnHubInsights.ts`
```ts
import { ref,
