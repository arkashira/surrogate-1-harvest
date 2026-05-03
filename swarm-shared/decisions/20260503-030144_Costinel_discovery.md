# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard (sidebar or top banner area).
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, top 3 signals (anomalies/recommendations), last updated timestamp.
- **CDN-first data fetch** with **local static fallback**:
  - Primary: `https://huggingface.co/datasets/{repo}/resolve/main/hubs/{hubName}/latest.json` (no auth, bypasses API rate limits).
  - Fallback: `public/data/top-hub-{hubName}.json` (committed to repo) for deterministic offline behavior and tests.
- Graceful degradation: if CDN fails, uses local static file; if that fails, uses bundled placeholder. Never crashes.
- Zero backend changes; pure frontend addition deployable via existing Docker/Vite pipeline.

---

### File changes (concrete)

#### 1) Env config
```bash
# .env or docker-compose env
VITE_HUB_NAME=MOC
VITE_HUB_CDN_BASE=https://huggingface.co/datasets/axentx/hubs/resolve/main
```

#### 2) Static fallback data
`public/data/top-hub-moc.json`
```json
{
  "hubName": "MOC",
  "description": "Top cross-account cost anomalies and RI coverage opportunities detected in the last 24h.",
  "topSignals": [
    {
      "id": "sig-001",
      "type": "anomaly",
      "severity": "critical",
      "title": "Unusual EC2 spend in prod-east-1",
      "description": "37% increase vs 7-day baseline; check auto-scaling and idle instances.",
      "category": "cost",
      "impact": "high"
    },
    {
      "id": "sig-002",
      "type": "recommendation",
      "severity": "info",
      "title": "RI coverage below target",
      "description": "Only 62% RI coverage for m5 family; consider purchase before price increase.",
      "category": "ri",
      "impact": "medium"
    },
    {
      "id": "sig-003",
      "type": "anomaly",
      "severity": "warning",
      "title": "Orphaned EBS volumes",
      "description": "12 unattached volumes across accounts; potential savings $1.2k/mo.",
      "category": "cleanup",
      "impact": "low"
    }
  ],
  "updatedAt": "2026-05-03T03:00:00Z"
}
```

#### 3) Panel component: `src/components/TopHubSignalPanel.vue`
```vue
<template>
  <section v-if="panelData || loading || error" class="top-hub-panel" :class="{ loading, error: !!error }">
    <header class="panel-header">
      <h3>{{ panelData?.hubName ?? hubName }} <span class="badge">Top Hub</span></h3>
      <span class="updated-at" v-if="panelData?.updatedAt">Updated {{ relativeTime(panelData.updatedAt) }}</span>
    </header>

    <div v-if="loading" class="placeholder">
      <div class="skeleton-line" />
      <div class="skeleton-line short" />
      <div class="skeleton-line" />
    </div>

    <div v-else-if="error" class="error-msg">
      <strong>Unable to load hub signals</strong>
      <p>{{ error }}</p>
      <small>Showing cached defaults.</small>
    </div>

    <div v-else-if="panelData" class="signals">
      <div class="hub-desc">{{ panelData.description }}</div>
      <ul class="signal-list">
        <li v-for="(s, i) in panelData.topSignals" :key="i" class="signal-item">
          <span class="signal-icon" :class="s.severity">{{ severityIcon(s.severity) }}</span>
          <div class="signal-body">
            <div class="signal-title">{{ s.title }}</div>
            <div class="signal-desc">{{ s.description }}</div>
            <div class="signal-meta">{{ s.category }} · {{ s.impact }}</div>
          </div>
        </li>
      </ul>
    </div>

    <div v-else class="no-data">
      No data available.
    </div>
  </section>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'

const props = defineProps({
  hubName: { type: String, default: import.meta.env.VITE_HUB_NAME || 'MOC' },
  cdnBase: { type: String, default: import.meta.env.VITE_HUB_CDN_BASE || 'https://huggingface.co/datasets/axentx/hubs/resolve/main' }
})

const panelData = ref(null)
const loading = ref(false)
const error = ref(null)

async function fetchHubData() {
  loading.value = true
  error.value = null
  const cdnUrl = `${props.cdnBase}/hubs/${props.hubName}/latest.json`
  const localUrl = `/data/top-hub-${props.hubName.toLowerCase()}.json`

  // 1) Try CDN
  try {
    const res = await fetch(cdnUrl, { cache: 'no-store' })
    if (!res.ok) throw new Error(`CDN ${res.status}`)
    const json = await res.json()
    panelData.value = normalize(json)
    return
  } catch (err) {
    console.warn('[TopHubPanel] CDN failed, trying local fallback:', err.message)
  }

  // 2) Try local static file
  try {
    const res = await fetch(localUrl, { cache: 'default' })
    if (!res.ok) throw new Error(`Local ${res.status}`)
    const json = await res.json()
    panelData.value = normalize(json)
    return
  } catch (err) {
    console.warn('[TopHubPanel] Local fallback failed:', err.message)
  }

  // 3) Use bundled placeholder
  panelData.value = localPlaceholder(props.hubName)
  error.value = 'Using default data'
  loading.value = false
}

function normalize(json) {
  return {
    hubName: json.hubName || json.hub || props.hubName,
    description: json.description || 'Knowledge hub insights.',
    topSignals: Array.isArray(json.topSignals || json.signals)
      ? (json.topSignals || json.signals).slice(0, 3)
      : [],
    updatedAt: json.updatedAt || new Date().toISOString()
  }
}

function localPlaceholder(name) {
  return {
    hubName: name,
    description: 'Top connected hub — review for contextual insights before planning tasks.',
    topSignals: [
      { title: 'Review MOC dependencies', description: 'High connectivity detected; validate downstream impacts.', category: 'graph', impact: 'high', severity: 'warning' },
      { title: 'Governance signals pending', description: 'Some proposals require human review.', category: 'governance', impact: 'medium', severity: 'info' }
    ],
    updatedAt: new Date().toISOString()
  }
}

function severityIcon(s) {
  return { warning: '⚠', info: 'ℹ', critical: '🚨', success: '✓', high: '🚨' }[s] || '•'
}

function relativeTime(iso) {
  const d = new Date(iso)
  const now = Date.now()
  const diff = Math.round((now - d) / 60000)
  if (diff < 1) return 'now'
  if (diff < 60) return `${diff}m ago`
  if (diff < 1440) return `${Math.round(diff / 60)}h ago`
  return `${Math.round(diff / 1440)}d ago`
}

watch(() => props.hubName, () => fetchHubData())
onMounted(fetchHubData)
</script>

<style scoped>
.top-hub
