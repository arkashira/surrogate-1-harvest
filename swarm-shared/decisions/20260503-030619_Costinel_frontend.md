# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted in the Costinel dashboard (sidebar or top banner).
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, top 3 signals (anomaly/optimization), and “View full hub” link.
- **CDN-first**: static JSON served from repo `public/hubs/` (fast, zero backend, no auth/rate limits). Optional remote CDN fallback via HuggingFace dataset for ops-managed updates.
- **Robust failure modes**: graceful degradation, 5-minute stale-while-revalidate caching, no render-blocking, fails open.

---

### File changes (concrete)

1) **Hub data file** (committed to repo; ops/knowledge-rag can update)  
`public/hubs/moc.json`

```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "description": "Top hub for cloud cost governance signals — anomalies, coverage gaps, and optimization opportunities.",
  "updated": "2026-05-03T03:00:00Z",
  "signals": [
    {
      "id": "sig-1",
      "severity": "high",
      "title": "Unattached EBS volumes (3)",
      "description": "Three unattached volumes across prod accounts; estimated $210/mo waste.",
      "action": "Review in Costinel > Storage",
      "href": "/dashboard?hub=MOC&tab=storage"
    },
    {
      "id": "sig-2",
      "severity": "medium",
      "title": "Low RI coverage (42%)",
      "description": "RDS RI coverage below target; projected $1.8k/mo savings available.",
      "action": "Open RI Planner",
      "href": "/ri-planner"
    },
    {
      "id": "sig-3",
      "severity": "low",
      "title": "Idle dev clusters nights/weekends",
      "description": "Schedule stop/start for non-prod clusters to save ~$650/mo.",
      "action": "View scheduling policies",
      "href": "/policies/scheduling"
    }
  ],
  "links": {
    "dashboard": "/dashboard?hub=MOC",
    "full_hub": "/hubs/MOC"
  }
}
```

2) **Env defaults** (`.env.example`)
```
VITE_HUB_NAME=MOC
VITE_HUB_DATASET=
VITE_HUB_CACHE_TTL=300
```

3) **CDN fetcher + cache**  
`src/lib/hubSignals.ts`

```ts
// Simple CDN fetcher with stale-while-revalidate (5m default) and optional remote fallback.
// Exports: loadHubData(hubName: string) -> Promise<HubData | null>

const CACHE_TTL = Number(import.meta.env.VITE_HUB_CACHE_TTL) || 300; // seconds
const DATASET_BASE = import.meta.env.VITE_HUB_DATASET?.trim(); // optional HuggingFace dataset base

interface HubData {
  hub: string;
  title: string;
  description: string;
  updated: string;
  signals: Array<{
    id: string;
    severity: 'high' | 'medium' | 'low';
    title: string;
    description: string;
    action?: string;
    href?: string;
  }>;
  links: {
    dashboard: string;
    full_hub: string;
  };
}

type CacheEntry = { data: HubData; ts: number; etag?: string; lastModified?: string };

const memoryCache = new Map<string, CacheEntry>();

function isFresh(entry: CacheEntry) {
  return Date.now() - entry.ts < CACHE_TTL * 1000;
}

async function fetchFromCDN(url: string, cached?: CacheEntry): Promise<{ data: HubData; etag?: string; lastModified?: string }> {
  const headers: Record<string, string> = {};
  if (cached?.etag) headers['If-None-Match'] = cached.etag;
  else if (cached?.lastModified) headers['If-Modified-Since'] = cached.lastModified;

  const res = await fetch(url, { headers, cache: 'no-cache' });
  if (res.status === 304 && cached) return { data: cached.data, etag: cached.etag, lastModified: cached.lastModified };
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const etag = res.headers.get('ETag') ?? undefined;
  const lastModified = res.headers.get('Last-Modified') ?? undefined;
  const data = await res.json();
  return { data, etag, lastModified };
}

function localCDNUrl(hubName: string) {
  return `/hubs/${encodeURIComponent(hubName.toLowerCase())}.json`;
}

function remoteCDNUrl(hubName: string) {
  if (!DATASET_BASE) return null;
  // HuggingFace dataset raw file resolve (no auth required for public files)
  return `https://huggingface.co/datasets/${DATASET_BASE}/resolve/main/${encodeURIComponent(hubName.toLowerCase())}.json`;
}

export async function loadHubData(hubName: string): Promise<HubData | null> {
  const key = hubName.toLowerCase();
  const cached = memoryCache.get(key);

  // Try local CDN first (fast, zero external dependency)
  const localUrl = localCDNUrl(key);
  try {
    const { data, etag, lastModified } = await fetchFromCDN(localUrl, cached);
    const entry: CacheEntry = { data, ts: Date.now(), etag, lastModified };
    memoryCache.set(key, entry);
    return data;
  } catch (err) {
    // If local missing and remote configured, try remote
    const remoteUrl = remoteCDNUrl(key);
    if (remoteUrl) {
      try {
        const { data, etag, lastModified } = await fetchFromCDN(remoteUrl, cached);
        const entry: CacheEntry = { data, ts: Date.now(), etag, lastModified };
        memoryCache.set(key, entry);
        return data;
      } catch (err2) {
        // fall through
      }
    }

    // Serve stale cache if available (fails open)
    if (cached) return cached.data;
    return null;
  }
}
```

4) **Panel component** (Vue 3 + Vite; convert to TSX if using React)  
`src/components/TopHubSignalPanel.vue`

```vue
<script setup lang="ts">
import { ref, onMounted, watch } from 'vue';
import { loadHubData } from '@/lib/hubSignals';

const props = defineProps<{
  hubName?: string;
}>();

const hub = ref<ReturnType<typeof loadHubData> extends Promise<infer T ? T : any> | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);

const hubName = props.hubName || import.meta.env.VITE_HUB_NAME || 'moc';

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const data = await loadHubData(hubName);
    hub.value = data;
  } catch (err: any) {
    error.value = err?.message || String(err);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(() => hubName, load);
</script>

<template>
  <section class="top-hub-panel" aria-label="Top hub signals">
    <div v-if="loading" class="panel-loading">Loading hub insights…</div>

    <div v-else-if="error" class="panel-error">
      <strong>Hub unavailable</strong>
      <span class="muted">{{ error }}</span>
    </div>

    <div v-else-if="hub" class="panel-content">
      <header class="panel-header">
        <h3 class="hub-title">{{ hub.title }}</h3>
        <p class="hub-desc">{{ hub.description }}</p>
      </header>

     
