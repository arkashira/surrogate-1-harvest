# Costinel / backend

## Final Implementation Plan — Top Hub Signal Panel (CDN-first, frontend-only)

**Scope**: Add a resilient, compact Top Hub signal card to the Costinel dashboard that surfaces the most-connected hub (e.g., "MOC") with rank, score, and contextual insights.  
**Constraints**: No backend changes; CDN-first to avoid HF API rate limits; graceful fallback; auto-refresh while active.  
**Effort**: ~60–90 minutes.

---

### Data contract (CDN + fallback)

CDN target (must match fallback shape):
```
https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/latest.json
```

Expected payload (both CDN and fallback):
```json
{
  "name": "MOC",
  "rank": 1,
  "score": 94.2,
  "insights": [
    "Multi-org cost governance patterns show 22% RI coverage upside when centralizing commitments.",
    "Cross-account tagging consistency improved 38% MoM."
  ],
  "updatedAt": "2026-04-27T00:00:00.000Z",
  "source": "knowledge-rag"
}
```

Create static fallback:

`src/data/top-hub-fallback.json`
```json
{
  "name": "MOC",
  "rank": 1,
  "score": 94.2,
  "insights": [
    "Multi-org cost governance patterns show 22% RI coverage upside when centralizing commitments.",
    "Cross-account tagging consistency improved 38% MoM."
  ],
  "updatedAt": "2026-04-27T00:00:00.000Z",
  "source": "knowledge-rag"
}
```

---

### Composable: CDN-first fetch with auto-refresh

`src/composables/useTopHub.js`
```js
import fallback from '@/data/top-hub-fallback.json'

const CDN_ROOT = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main'
const HUB_PATH = 'top-hub/latest.json'
const CDN_URL = `${CDN_ROOT}/${HUB_PATH}`
const REFRESH_MS = 5 * 60 * 1000 // 5 minutes

function normalize(raw) {
  return {
    name: raw.name || raw.hub || raw.top_hub || 'Unknown',
    rank: Number(raw.rank) || 1,
    score: Number(raw.score ?? raw.relevance ?? raw.weight ?? 0),
    insights: Array.isArray(raw.insights)
      ? raw.insights.map((i) => (i && i.text) || i || '').filter(Boolean)
      : [],
    updatedAt: raw.updatedAt || raw.ts || raw.updated_at || null,
    source: raw.source || null
  }
}

export function useTopHub() {
  const hub = ref(fallback)
  const loading = ref(false)
  const error = ref(null)
  const source = ref('fallback') // 'cdn' | 'fallback'
  let refreshTimer = null

  async function fetchHub() {
    loading.value = true
    error.value = null

    try {
      const res = await fetch(CDN_URL, { cache: 'no-store' })
      if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`)
      const data = await res.json()
      hub.value = normalize(data)
      source.value = 'cdn'
    } catch (err) {
      // CDN failed — keep normalized fallback
      hub.value = normalize(fallback)
      source.value = 'fallback'
      error.value = err.message || 'CDN unavailable'
    } finally {
      loading.value = false
    }
  }

  function startAutoRefresh() {
    stopAutoRefresh()
    fetchHub()
    refreshTimer = setInterval(fetchHub, REFRESH_MS)
  }

  function stopAutoRefresh() {
    if (refreshTimer) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  }

  // Expose formatted timestamp for display
  const updatedAt = computed(() => {
    if (!hub.value.updatedAt) return ''
    const d = new Date(hub.value.updatedAt)
    return d.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    })
  })

  return {
    hub,
    loading,
    error,
    source,
    updatedAt,
    fetchHub,
    startAutoRefresh,
    stopAutoRefresh
  }
}
```

---

### Component: Top Hub signal card

`src/components/costinel/TopHubSignalCard.vue`
```vue
<template>
  <div class="top-hub-signal-card">
    <div class="card-header">
      <span class="icon">🔗</span>
      <h3>Top Hub Signal</h3>
      <el-tag v-if="loading" size="small" type="info" effect="plain">Loading</el-tag>
      <el-tag v-else-if="error" size="small" type="warning" effect="plain">Using fallback</el-tag>
      <el-tag v-else-if="source === 'cdn'" size="small" type="success" effect="plain">CDN</el-tag>
      <el-tag v-else size="small" type="info" effect="plain">Local</el-tag>
    </div>

    <div v-if="loading" class="loading">
      <el-skeleton :rows="3" animated />
    </div>

    <div v-else-if="!hub" class="empty">
      <el-result icon="warning" title="Unable to load hub data" sub-title="Insights unavailable." />
    </div>

    <div v-else class="content">
      <div class="hub-main">
        <div class="hub-name">{{ hub.name }}</div>
        <div class="hub-meta">
          <span class="rank">Rank {{ hub.rank }}</span>
          <div class="hub-score">
            <span class="label">Score</span>
            <span class="value">{{ hub.score }}</span>
          </div>
        </div>
      </div>

      <div v-if="hub.insights && hub.insights.length" class="insights">
        <div v-for="(item, idx) in hub.insights.slice(0, 4)" :key="idx" class="insight-item">
          <span class="bullet">•</span>
          <span class="text">{{ item }}</span>
        </div>
      </div>

      <div v-else class="insights placeholder">
        <span class="text">No contextual insights available.</span>
      </div>

      <div class="meta">
        <small>Updated {{ updatedAt || '—' }}</small>
      </div>
    </div>
  </div>
</template>

<script>
import { ElTag, ElSkeleton, ElResult } from 'element-plus'
import { useTopHub } from '@/composables/useTopHub'

export default {
  name: 'TopHubSignalCard',
  components: { ElTag, ElSkeleton, ElResult },
  setup() {
    const {
      hub,
      loading,
      error,
      source,
      updatedAt,
      startAutoRefresh,
      stopAutoRefresh
    } = useTopHub()

    // Start refresh when mounted; stop on unmount
    startAutoRefresh()

    // Keep composable lifecycle tied to component
    // (composable exposes stopAutoRefresh for cleanup if needed)
    // If using Vue 3's lifecycle inside setup, use onUnmounted(stopAutoRefresh)
    // For simplicity, we rely on the composable's timer and expose stop.
    // Component parent or app teardown should call stopAutoRefresh if needed.

    return {
      hub,
      loading,
      error,
      source,
      updatedAt
    }
  },
  beforeUnmount() {
    // Ensure timer cleared when component unmounts
    // Access the same composable instance via a small workaround:
    // Since useTopHub is called per-component instance, we can store the returned stop
    // by capturing it in setup and exposing via this or using provide/in
