# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

**Core decision**: Use **public CDN JSON** (HuggingFace) as the primary source with a **local public stub** as fallback. This combines Candidate 1’s CDN strategy (bypasses HF API limits, no auth) with Candidate 2’s local seed file (reproducible dev/test, instant fallback).

---

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted at the top of `/dashboard`.
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, top 3 actionable signals, last-updated timestamp.
- **CDN-first data strategy**:
  - Primary: `https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/hubs/{hub}.json`
  - No Authorization header; avoids HF API rate limits.
- **Graceful fallback**:
  - If CDN fails or times out, load `public/data/hubs/{hub}.json` (local seed).
  - If that fails, show minimal inline stub (no crash, no console error for users).
- **Zero backend changes**; frontend-only feature.
- **Mobile responsive** and non-blocking (panel never stalls dashboard).

---

### File changes (concrete)

#### 1) Seed hub data (local fallback)
`public/data/hubs/moc.json`
```json
{
  "hub": "moc",
  "title": "MOC — Map of Cost",
  "description": "High-level topology of cloud cost flows and ownership. Primary reference for cost governance signals.",
  "signals": [
    {
      "id": "moc-01",
      "title": "Unattached EBS >30d",
      "severity": "high",
      "action": "Review and snapshot or delete unattached volumes in us-east-1 / prod.",
      "context": "3 volumes (~$180/mo) unattached >30 days."
    },
    {
      "id": "moc-02",
      "title": "Idle RDS instances",
      "severity": "medium",
      "action": "Downsize or schedule stop/start for non-prod RDS during nights/weekends.",
      "context": "2 db.t3.medium instances <10% avg CPU."
    },
    {
      "id": "moc-03",
      "title": "Over-provisioned node pools",
      "severity": "medium",
      "action": "Reduce node pool sizes by 1 instance type where CPU <30% sustained.",
      "context": "GKE us-central1: 4 n2-standard-4 nodes; can shift to n2-standard-2."
    }
  ],
  "updatedAt": "2026-05-03T03:00:00Z",
  "source": "costinel-hubs"
}
```

#### 2) Environment config
`.env`
```
VITE_HUB_NAME=MOC
VITE_HUB_CDN_BASE=https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/hubs
```

#### 3) CDN + fallback fetch utility
`src/lib/fetchHubData.ts`
```ts
const HUB_NAME = (import.meta.env.VITE_HUB_NAME || 'MOC').toLowerCase();
const CDN_BASE = import.meta.env.VITE_HUB_CDN_BASE || 'https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/hubs';
const CDN_URL = `${CDN_BASE}/${HUB_NAME}.json`;
const LOCAL_URL = `/data/hubs/${HUB_NAME}.json`;

const TIMEOUT_MS = 5000;

async function fetchWithTimeout(url: string, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    clearTimeout(id);
    return res;
  } catch (err) {
    clearTimeout(id);
    throw err;
  }
}

async function tryFetch(url: string): Promise<any | null> {
  try {
    const res = await fetchWithTimeout(url, TIMEOUT_MS);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch {
    return null;
  }
}

export async function fetchHubData(): Promise<any | null> {
  // 1) Try CDN
  const cdnData = await tryFetch(CDN_URL);
  if (cdnData) return cdnData;

  // 2) Try local public file
  const localData = await tryFetch(LOCAL_URL);
  if (localData) return localData;

  // 3) Return null so UI can show minimal stub
  console.warn('[HubPanel] All data sources failed for hub:', HUB_NAME);
  return null;
}
```

#### 4) TopHubSignalPanel component (framework-agnostic logic; adapt to Vue/Svelte/React)
Key behaviors:
- Loads asynchronously without blocking dashboard render.
- Shows minimal stub if no data.
- Accessible labels and keyboard-friendly.

Example (Vue 3):
`src/components/TopHubSignalPanel.vue`
```vue
<template>
  <section class="top-hub-panel" aria-label="Top hub signals">
    <header class="top-hub-panel__header">
      <span class="top-hub-panel__badge">{{ hub.hub?.toUpperCase() || 'MOC' }}</span>
      <h3 class="top-hub-panel__title">{{ hub.title || 'Multi-Org Cost Signals' }}</h3>
      <p class="top-hub-panel__desc">{{ hub.description || 'Cross-account reserved coverage and idle resource signals.' }}</p>
      <p class="top-hub-panel__updated" v-if="hub.updatedAt">Updated {{ formatDate(hub.updatedAt) }}</p>
    </header>

    <ul class="top-hub-panel__signals" aria-label="Top actionable signals">
      <li
        v-for="s in hub.signals || stubSignals"
        :key="s.id"
        :class="['signal', s.severity]"
      >
        <strong class="signal__title">{{ s.title }}</strong>
        <span class="signal__action">{{ s.action }}</span>
        <span v-if="s.context" class="signal__context">{{ s.context }}</span>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { fetchHubData } from '@/lib/fetchHubData';

const raw = ref<any>(null);

onMounted(async () => {
  raw.value = await fetchHubData();
});

const hub = computed(() => raw.value || {});

const stubSignals = [
  { id: 'stub-1', severity: 'high', title: 'Loading signals...', action: '', context: '' }
];

function formatDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  });
}
</script>

<style scoped>
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 10px;
  padding: 14px 18px;
  background: #fbfdff;
  margin-bottom: 16px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}
.top-hub-panel__header { margin-bottom: 10px; }
.top-hub-panel__badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  background: #0ea5e9;
  color: white;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.top-hub-panel__title {
  margin: 6px 0 4px;
  font-size: 16px;
  font-weight: 600;
  color: #0f172a;
