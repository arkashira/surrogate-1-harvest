# Costinel / discovery

## Final Implementation Plan: Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel surfacing the most-connected hub and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Files touched**:  
- `src/components/dashboard/TopHubSignalPanel.vue` (new)  
- `src/views/Dashboard.vue` (mount)  
- `src/services/knowledgeGraph.js` (lightweight service)  
- `src/locales/en.json` + `th.json` (i18n)  
- `public/data/knowledge-top-hub.json` (fallback)  

---

### 1) Lightweight knowledge-graph service (CDN-first, resilient)

`src/services/knowledgeGraph.js`
```js
// CDN-first, zero-runtime-API client for Top-hub signals.
// Prefer CDN; fallback to local static JSON. Read-only, resilient.

const CDN_BASE = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main';
const STATIC_FALLBACK = '/data/knowledge-top-hub.json';

export async function fetchTopHubSignal(dateFolder = 'latest') {
  // dateFolder example: '2026-05-03' or 'latest'
  const url = `${CDN_BASE}/${dateFolder}/top-hub.json`;

  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    return res.json();
  } catch (err) {
    // Fallback to static bundled JSON (safe, read-only)
    try {
      const fallbackRes = await fetch(STATIC_FALLBACK, { cache: 'no-store' });
      if (!fallbackRes.ok) throw new Error('Static fallback missing');
      return fallbackRes.json();
    } catch (fbErr) {
      console.warn('Top-hub signal unavailable', err, fbErr);
      return null;
    }
  }
}
```

`public/data/knowledge-top-hub.json` (minimal fallback)
```json
{
  "hub": "MOC",
  "score": 0.94,
  "label": "Mission Operations Center",
  "summary": "Central hub for cross-cloud cost governance signals.",
  "proposals": [
    {
      "id": "prop-ri-001",
      "title": "Increase RI coverage for prod us-east-1",
      "impact": "high",
      "estimatedSavingsUSD": 28400,
      "tags": ["RI", "AWS", "prod"]
    },
    {
      "id": "prop-st-002",
      "title": "Schedule non-prod clusters off-hours",
      "impact": "medium",
      "estimatedSavingsUSD": 9200,
      "tags": ["scheduling", "GCP", "non-prod"]
    }
  ],
  "lastUpdated": "2026-05-03T08:12:00Z"
}
```

---

### 2) TopHubSignalPanel component (read-only, i18n-ready)

`src/components/dashboard/TopHubSignalPanel.vue`
```vue
<template>
  <section class="top-hub-signal-panel card">
    <header class="panel-header">
      <h3 class="title">
        <span class="icon">🔗</span>
        {{ $t('topHub.title') }}
      </h3>
      <span v-if="loading" class="loading">{{ $t('topHub.loading') }}</span>
      <span v-else-if="!hub" class="empty">{{ $t('topHub.noData') }}</span>
    </header>

    <div v-if="hub" class="hub-meta">
      <div class="hub-name">{{ hub.label }}</div>
      <div class="hub-summary" v-if="hub.summary">{{ hub.summary }}</div>
      <div class="hub-stats">
        <span class="stat">
          <strong>{{ hub.score ?? hub.connections ?? '-' }}</strong>
          {{ $t('topHub.score') }}
        </span>
        <span v-if="hub.lastUpdated" class="updated">
          {{ $t('topHub.updated') }}: {{ formatDate(hub.lastUpdated) }}
        </span>
      </div>
    </div>

    <div v-if="hub && hub.proposals && hub.proposals.length" class="proposals-list">
      <article v-for="p in hub.proposals" :key="p.id" class="proposal-item">
        <div class="proposal-header">
          <h4 class="proposal-title">{{ p.title }}</h4>
          <span v-if="p.impact" class="impact-badge" :class="p.impact">{{ p.impact }}</span>
        </div>
        <p v-if="p.summary || p.context" class="proposal-context">{{ p.summary || p.context }}</p>
        <div class="proposal-meta">
          <span v-if="p.estimatedSavingsUSD" class="savings">
            ${{ p.estimatedSavingsUSD.toLocaleString() }}
          </span>
          <span v-if="p.tags?.length" class="tags">
            <span v-for="t in p.tags" :key="t" class="tag">{{ t }}</span>
          </span>
          <span class="proposal-id">#{{ p.id }}</span>
        </div>
      </article>
    </div>

    <div v-else-if="!loading && hub" class="no-proposals">
      {{ $t('topHub.noProposals') }}
    </div>
  </section>
</template>

<script>
import { fetchTopHubSignal } from '@/services/knowledgeGraph';

export default {
  name: 'TopHubSignalPanel',
  data() {
    return {
      loading: false,
      hub: null,
    };
  },
  mounted() {
    this.loading = true;
    fetchTopHubSignal()
      .then((data) => {
        this.hub = data;
      })
      .finally(() => {
        this.loading = false;
      });
  },
  methods: {
    formatDate(iso) {
      try {
        return new Intl.DateTimeFormat(undefined, {
          dateStyle: 'medium',
          timeStyle: 'short',
        }).format(new Date(iso));
      } catch {
        return iso;
      }
    },
  },
};
</script>

<style scoped>
.top-hub-signal-panel {
  border-left: 4px solid #4caf50;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}
.title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 1rem;
  margin: 0;
}
.hub-meta {
  margin-bottom: 0.75rem;
}
.hub-name {
  font-weight: 700;
  font-size: 1.125rem;
}
.hub-summary {
  color: #555;
  font-size: 0.875rem;
  margin-bottom: 0.25rem;
}
.hub-stats {
  color: #666;
  font-size: 0.875rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}
.updated {
  color: #888;
  font-size: 0.75rem;
}
.proposals-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.proposal-item {
  padding: 0.5rem;
  border: 1px solid #eee;
  border-radius: 6px;
  background: #fafafa;
}
.proposal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.proposal-title {
  margin: 0 0 0.25rem 0
