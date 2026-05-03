# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: <2h (frontend-only, zero backend changes).  
**Entry point**: `src/components/CostinelTopHubCard.vue` + placement in Cost dashboard sidebar or “Insights” panel.

---

### 1) Design & UX (resolved)
- **Card title**: **Top-Hub Signal**
- **Subtitle**: Most-connected hub + short context blurb
- **Hub display**: Hub label, description, and connection count badge (not centrality score)
- **Signals**: 3 signals shown as compact clickable list items (not pills) to accommodate title + summary + tags without wrapping issues.
  - Signal title (clickable)
  - 1-line summary
  - Tags (e.g. `#cost-optimization`, `#knowledge-rag`, `#graph`)
- **States**:
  - **Loading**: Skeleton shimmer (3 rows)
  - **Empty**: “No hub signals available — run market analysis + knowledge-rag to populate.”
  - **Error**: Non-blocking toast + inline muted message (do not crash dashboard)
- **Interaction**:
  - Clicking a signal opens detail in a drawer/modal or navigates to the linked page (read-only).
  - Copy-to-clipboard for hub name / signal IDs (optional but recommended).
- **Visuals**:
  - Use existing design tokens.
  - Icons: graph/network node for hub; document/external-link for signals.

---

### 2) Data contract (frontend expectation)
Consume a static JSON fixture during dev (committed to `public/mock/top-hub.json`) and a lightweight endpoint in prod:

```
GET /api/knowledge-rag/top-hub
```

**Response shape** (frontend tolerates extra fields):

```json
{
  "hub": {
    "id": "MOC",
    "label": "MOC",
    "connections": 42,
    "description": "Master operational context — central to cost governance decisions."
  },
  "signals": [
    {
      "id": "sig-001",
      "title": "RI coverage gap in us-east-1",
      "summary": "Detected 38% RI coverage shortfall for m5 family; projected 22% cost uplift next quarter.",
      "tags": ["#cost-optimization", "#knowledge-rag", "#graph"],
      "href": "/insights/ri-coverage?hub=MOC"
    },
    {
      "id": "sig-002",
      "title": "Orphaned EBS volumes",
      "summary": "11 unattached volumes (~$340/mo) identified across dev accounts.",
      "tags": ["#cleanup", "#storage", "#knowledge-rag"],
      "href": "/cleanup/ebs?hub=MOC"
    },
    {
      "id": "sig-003",
      "title": "Idle dev clusters nights/weekends",
      "summary": "Non-prod clusters idle 65% of time; estimated $1.2k/mo savings with scheduled stop/start.",
      "tags": ["#governance", "#scheduling", "#knowledge-rag"],
      "href": "/governance/schedules?hub=MOC"
    }
  ],
  "generatedAt": "2026-05-03T03:15:00.000Z"
}
```

**Dev fixture**: `public/mock/top-hub.json` (same shape as above).

---

### 3) Implementation steps (90 min)

#### A) Add mock data (5 min)
Create `public/mock/top-hub.json` with the fixture above.

#### B) Create `CostinelTopHubCard.vue` (40 min)

```vue
<template>
  <div class="top-hub-card card">
    <!-- Header -->
    <div class="card-header">
      <div class="hub-title">
        <IconGraph class="icon" />
        <div>
          <h3>{{ hub?.label || '—' }}</h3>
          <p class="hub-desc">{{ hub?.description || '' }}</p>
        </div>
      </div>
      <Badge v-if="hub?.connections" :value="hub.connections + ' connections'" />
    </div>

    <!-- Loading -->
    <div v-if="loading" class="signals-skeleton">
      <div class="skeleton-row" v-for="i in 3" :key="i"></div>
    </div>

    <!-- Empty -->
    <div v-else-if="!signals?.length" class="empty">
      No hub signals available — run market analysis + knowledge-rag to populate.
    </div>

    <!-- Signals -->
    <div v-else class="signals-list">
      <a
        v-for="s in signals"
        :key="s.id"
        :href="s.href"
        class="signal-item"
        @click.prevent="openSignal(s)"
      >
        <div class="signal-title">{{ s.title }}</div>
        <div class="signal-summary">{{ s.summary }}</div>
        <div class="signal-tags" v-if="s.tags?.length">
          <span v-for="tag in s.tags" :key="tag" class="tag">{{ tag }}</span>
        </div>
        <ChevronRight class="chevron" />
      </a>
    </div>

    <!-- Timestamp -->
    <div v-if="data?.generatedAt" class="meta">
      Updated {{ formatTime(data.generatedAt) }}
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';
import { useToast } from '@/composables/useToast';
import IconGraph from '@/icons/IconGraph.vue';
import ChevronRight from '@/icons/ChevronRight.vue';
import Badge from '@/components/Badge.vue';

const props = defineProps<{
  apiUrl?: string;
}>();

const data = ref(null);
const loading = ref(false);
const toast = useToast();

const hub = computed(() => data.value?.hub);
const signals = computed(() => data.value?.signals);

async function fetchTopHub() {
  loading.value = true;
  try {
    const url = props.apiUrl || '/api/knowledge-rag/top-hub';
    const res = await fetch(url).catch(() => null);
    if (res?.ok) {
      data.value = await res.json();
    } else {
      const mockRes = await fetch('/mock/top-hub.json');
      data.value = await mockRes.json();
    }
  } catch (err) {
    toast.error('Failed to load hub signals');
    console.error(err);
  } finally {
    loading.value = false;
  }
}

function openSignal(signal) {
  // Read-only: open link in same tab or trigger drawer as needed
  if (signal.href) {
    window.open(signal.href, '_self');
  }
}

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
}

onMounted(fetchTopHub);
</script>

<style scoped>
.top-hub-card { padding: 16px; border-radius: 8px; }

.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.hub-title { display: flex; gap: 10px; align-items: flex-start; }
.hub-title h3 { margin: 0; font-size: 16px; }
.hub-desc { margin: 2px 0 0; color: var(--text-muted); font-size: 13px; }

.signals-list { display: flex; flex-direction: column; gap: 8px; }

.signal-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--bg-subtle);
  text-decoration:
