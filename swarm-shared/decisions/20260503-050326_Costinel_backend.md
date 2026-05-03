# Costinel / backend

## Final Implementation Plan — Top Hub Signal Panel  
**CDN-first, frontend-only, resilient, ~60–90 min**

**Core goals**
- Show the most-connected hub (e.g., “MOC”) with key signals on the Costinel dashboard.
- No backend changes; deploy immediately.
- CDN-delivered JSON with graceful fallback, stale-while-revalidate, and manual refresh.
- Deterministic behavior: CDN wins when available; local stub guarantees UI never breaks.

---

### 1) Public data file (CDN + local fallback)
Create `/public/data/top-hub.json` (committed; will be overwritten daily by orchestrator).

```json
{
  "hub": "MOC",
  "rank": 1,
  "connections": 1247,
  "score": 94,
  "label": "Most-connected hub",
  "insights": [
    "Cross-account IAM roles dominate connections (38%)",
    "Unattached EBS volumes present cost-drift risk in 3 accounts",
    "Tag coverage below 60% in linked projects"
  ],
  "recommendations": [
    "Schedule RI purchase for top 3 linked services",
    "Enable SCP guardrails for tag enforcement"
  ],
  "lastUpdated": "2026-05-03T04:58:00.000Z",
  "source": "knowledge-rag#graph"
}
```

Notes:
- `lastUpdated` (ISO 8601) is canonical for display and staleness checks.
- `rank`, `connections`, and `score` are included for flexibility; UI can choose which to show.
- Keep file small and CDN-cacheable (no auth).

---

### 2) Composable: `src/composables/useTopHub.js`
Robust fetch with CDN-first, local fallback, SWR, and manual refresh.

```js
import { ref, computed } from 'vue'

const DAY = 24 * 60 * 60 * 1000
const DEFAULT_STALE_TTL = 6 * 60 * 60 * 1000 // 6h

// Local stub used only if CDN + local fetch both fail
import localTopHub from '@/data/local-top-hub.json'

export function useTopHub(options = {}) {
  const {
    staleWhileRevalidateMs = DEFAULT_STALE_TTL,
    cdnPath = '/data/top-hub.json',
    localPath = '/data/local-top-hub.json'
  } = options

  const data = ref(null)
  const loading = ref(false)
  const error = ref(null)
  const lastFetch = ref(null)
  const source = ref('local') // 'cdn' | 'local'

  const isStale = computed(() => {
    if (!lastFetch.value) return true
    return Date.now() - lastFetch.value > staleWhileRevalidateMs
  })

  async function tryFetch(url, opts = {}) {
    const res = await fetch(url, { cache: 'no-store', ...opts })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  async function fetchTopHub(force = false) {
    if (!force && !isStale.value && data.value) return

    loading.value = true
    error.value = null

    try {
      // CDN-first
      const json = await tryFetch(cdnPath + `?t=${Date.now()}`)
      data.value = json
      source.value = 'cdn'
    } catch (err) {
      // Fallback to local copy in public/ (served at same path)
      try {
        const json = await tryFetch(localPath + `?t=${Date.now()}`)
        data.value = json
        source.value = 'local'
      } catch (err2) {
        // Hard-coded local stub as last resort
        data.value = localTopHub
        source.value = 'local'
        error.value = err
      }
    } finally {
      lastFetch.value = Date.now()
      loading.value = false
    }
  }

  // Initial best-effort load
  fetchTopHub()

  return {
    data,
    loading,
    error,
    isStale,
    source,
    lastFetch,
    fetchTopHub
  }
}
```

Create `/public/data/local-top-hub.json` as a minimal safe stub (same schema) and a matching `src/data/local-top-hub.json` for the import fallback.

---

### 3) Component: `src/components/TopHubSignalCard.vue`
Unified, accessible, and deterministic rendering.

```vue
<template>
  <section class="top-hub-signal-card" aria-labelledby="hub-title">
    <header class="card-header">
      <div>
        <div class="badges">
          <span class="badge">Top Hub</span>
          <span class="label" v-if="data?.label">{{ data.label }}</span>
        </div>
        <p class="subtitle">Most-connected entity — governance context</p>
      </div>
      <div class="actions">
        <button
          class="btn-refresh"
          @click="refresh"
          :disabled="loading"
          aria-label="Refresh top hub data"
        >
          ↻
        </button>
      </div>
    </header>

    <div v-if="loading && !hub" class="skeleton">
      <div class="skeleton-line wide"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line medium"></div>
    </div>

    <div v-else-if="error && !hub" class="error">
      Unable to load Top Hub signal. Using local fallback.
      <button @click="refresh" class="btn-retry">Retry</button>
    </div>

    <div v-else-if="hub" class="content">
      <div class="hub-meta">
        <span class="hub-name">{{ hub.hub }}</span>
        <span v-if="hub.rank" class="hub-rank">Rank #{{ hub.rank }}</span>
        <span v-if="hub.score" class="hub-score">Signal {{ hub.score }}</span>
      </div>

      <div v-if="hub.connections != null" class="hub-stats">
        <div class="stat">
          <span class="stat-value">{{ formatNumber(hub.connections) }}</span>
          <span class="stat-label">connections</span>
        </div>
      </div>

      <div v-if="hub.insights?.length" class="insights">
        <h3>Insights</h3>
        <ul>
          <li v-for="(item, i) in hub.insights" :key="i">{{ item }}</li>
        </ul>
      </div>

      <div v-if="hub.recommendations?.length" class="recommendations">
        <h3>Recommendations</h3>
        <ul>
          <li v-for="(item, i) in hub.recommendations" :key="i">{{ item }}</li>
        </ul>
      </div>

      <footer class="card-footer">
        <small>
          Updated {{ formatDate(hub.lastUpdated) }}
          <span v-if="isStale" class="stale-badge">(stale)</span>
          • {{ sourceLabel }}
        </small>
      </footer>
    </div>
  </section>
</template>

<script>
import { useTopHub } from '@/composables/useTopHub'
import { format } from 'date-fns'

export default {
  name: 'TopHubSignalCard',
  setup() {
    const {
      data: hub,
      loading,
      error,
      isStale,
      source,
      fetchTopHub: refresh
    } = useTopHub({ staleWhileRevalidateMs: 6 * 60 * 60 * 1000 })

    const formatNumber = (n) => new Intl.NumberFormat().format(n)
    const formatDate = (iso) => format(new Date(iso), 'MMM d, yyyy HH:mm')
    const sourceLabel = computed(() => (source.value === 'cdn' ? 'CDN' : 'Local'))

    return {
      hub,
      loading,
      error,
      isStale,
