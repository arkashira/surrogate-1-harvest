# Costinel / quality

## Final Synthesis — Costinel Quality: Top-Hub Signal Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, concise rationale, and provenance; zero backend changes; CDN-friendly static asset pattern; mobile-responsive.

---

### 1) Highest-value incremental improvement
Add a persistent, read-only **Top-Hub Signal Card** to the Costinel quality view that:
- Uses a **local static JSON + client-side render** pattern (no backend/API changes).
- Shows hub name, connection count, short rationale (top 3 edges), and a “Signal” summary.
- Exposes an **expandable Audit** with provenance (source doc, timestamp, link).
- Uses CDN-bypass pattern for dataset fetches (static JSON from repo/CDN) and avoids HF API calls during render.
- Reuses existing Lightning Studio if available for any background compute (this card is frontend-only).
- Follows existing design tokens and is mobile-responsive.

---

### 2) Concrete implementation steps (≤2h)

#### 1) Locate quality view
Expected path: `/opt/axentx/Costinel/src/pages/Quality.vue` (or `Quality.tsx`). Confirm structure quickly.

#### 2) Add static JSON asset (precomputed by orchestration)
Create `/opt/axentx/Costinel/public/data/top-hub/latest.json`:
```json
{
  "hub": {
    "id": "MOC",
    "label": "MOC",
    "degree": 42,
    "edges": [
      { "target": "cost-anomaly", "label": "indicates", "weight": 0.92 },
      { "target": "ri-coverage", "label": "relates", "weight": 0.87 },
      { "target": "budget-drift", "label": "triggers", "weight": 0.81 }
    ]
  },
  "signal": "MOC centrality spike suggests governance decisions are tightly coupled to cost anomalies — prioritize policy reviews.",
  "rationale": [
    "Highest degree node (42 connections)",
    "Strong links to cost-anomaly and RI coverage",
    "Recent edge-weight growth +18% week-over-week"
  ],
  "audit": {
    "sourceDoc": "knowledge-rag://graph/2024-06-12",
    "timestamp": "2024-06-12T08:30:00Z",
    "link": "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub/latest.json"
  }
}
```

#### 3) Create TopHubSignalCard component
Create `/opt/axentx/Costinel/src/components/TopHubSignalCard.vue`:
```vue
<template>
  <section class="top-hub-signal-card" aria-label="Top hub signal">
    <header class="card-header">
      <h3>Top Hub Signal</h3>
      <span class="badge" v-if="data">{{ data.hub.label }}</span>
    </header>

    <div v-if="loading" class="skeleton">Loading signal…</div>
    <div v-else-if="error" class="muted">Signal unavailable</div>
    <div v-else-if="data" class="signal-body">
      <div class="hub-meta">
        <strong>{{ data.hub.label }}</strong>
        <span class="degree">{{ data.hub.degree }} connections</span>
      </div>
      <p class="signal">{{ data.signal }}</p>
      <ul class="rationale">
        <li v-for="(r, i) in data.rationale" :key="i">{{ r }}</li>
      </ul>

      <div class="audit-section">
        <button @click="showAudit = !showAudit" class="audit-toggle">
          {{ showAudit ? "Hide audit" : "Show audit" }}
        </button>
        <div v-if="showAudit" class="audit-details">
          <p><strong>Source:</strong> {{ data.audit.sourceDoc }}</p>
          <p><strong>Timestamp:</strong> {{ formatTime(data.audit.timestamp) }}</p>
          <p><strong>Link:</strong> <a :href="data.audit.link" target="_blank" rel="noopener">{{ data.audit.link }}</a></p>
        </div>
      </div>

      <footer class="card-footer muted">
        Sense + Signal — ไม่ Execute
      </footer>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';

interface HubNode {
  id: string;
  label: string;
  degree: number;
  edges: Array<{ target: string; label: string; weight?: number }>;
}

interface Audit {
  sourceDoc: string;
  timestamp: string;
  link: string;
}

interface TopHubSignal {
  hub: HubNode;
  signal: string;
  rationale: string[];
  audit: Audit;
}

const data = ref<TopHubSignal | null>(null);
const loading = ref(true);
const error = ref(false);
const showAudit = ref(false);

onMounted(async () => {
  try {
    // Static JSON from public/data (no backend)
    const res = await fetch('/data/top-hub/latest.json', { cache: 'no-store' });
    if (res.ok) {
      data.value = await res.json();
    } else {
      error.value = true;
    }
  } catch {
    error.value = true;
  } finally {
    loading.value = false;
  }
});

function formatTime(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'short',
    timeStyle: 'short'
  });
}
</script>

<style scoped>
.top-hub-signal-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  background: var(--bg-card);
}
.card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem; }
.badge { background:var(--accent); color:#fff; padding:0.25rem 0.6rem; border-radius:999px; font-size:0.8rem; }
.hub-meta { display:flex; gap:0.5rem; align-items:center; margin-bottom:0.5rem; }
.degree { color:var(--muted); font-size:0.9rem; }
.signal { margin:0.5rem 0; color:var(--text); }
.rationale { margin:0; padding-left:1.1rem; color:var(--muted); font-size:0.9rem; }
.audit-section { margin-top:0.5rem; }
.audit-toggle { background:none; border:none; color:var(--accent); cursor:pointer; padding:0; font-size:0.9rem; }
.audit-details { margin-top:0.5rem; padding:0.5rem; background:var(--bg-muted); border-radius:4px; font-size:0.85rem; }
.audit-details a { color:var(--accent); word-break:break-all; }
.card-footer { margin-top:0.5rem; font-size:0.8rem; }
.skeleton, .muted { color:var(--muted); }

/* Mobile responsive */
@media (max-width: 640px) {
  .top-hub-signal-card { padding:0.75rem; }
  .hub-meta { flex-direction:column; align-items:flex-start; gap:0.25rem; }
}
</style>
```

#### 4) Wire into Quality page
In `Quality.vue`, import and place card near top of quality section:
```vue
<script setup lang="ts">
import TopHubSignalCard from '@/components/TopHubSignalCard.vue';
</script>

<template>
  <main class="quality-page">
    <section class="quality-header">...</section>

    <!-- Read-only signal -->
    <TopHubSignalCard />

    <!-- existing quality panels below
