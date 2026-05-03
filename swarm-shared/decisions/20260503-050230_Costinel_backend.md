# Costinel / backend

### Highest-Value Incremental Improvement  
**Ship in <2h:** Add a CDN-first, frontend-only **Top Hub Signal Card** to the Costinel dashboard that surfaces the most-connected hub (e.g., “MOC”), connection count, and 3 contextual insights.  
**Why this wins:**  
- Delivers immediate user value (cost-governance prioritization) without backend changes.  
- Avoids HF API 429 limits by fetching from public CDN (`/resolve/main/`) instead of authenticated HF endpoints.  
- Degrades gracefully with a static fallback baked into the build.  
- Fits within 60–90 minutes (frontend-only) and is testable end-to-end.

**Do not implement Candidate 2’s HF rate-limit fix right now** unless you must keep recursive `list_repo_files` calls; the Top Hub card removes the need for those calls entirely in this feature.

---

### Concrete Implementation Plan (90 minutes total)

| Phase | Time | Action |
|-------|------|--------|
| **1. Scaffold & review** | 10 min | Create `src/components/CostinelTopHubCard.vue`, `src/utils/cdnHubClient.js`, and `src/assets/data/top-hub-fallback.json`. |
| **2. CDN client** | 15 min | Implement `fetchTopHubFromCDN()` with timeout + exponential backoff (no auth, cache-bust). |
| **3. Static fallback** | 5 min | Add minimal JSON fallback (hub, connections, insights[], updatedAt, source). |
| **4. Card component** | 25 min | Build resilient card: loading → data → error/fallback states; render hub name, connections, top 3 insights (tag + text). |
| **5. Dashboard integration** | 15 min | Import and mount `CostinelTopHubCard` in `src/views/Dashboard.vue` within the primary grid. |
| **6. Test & verify** | 20 min | Verify CDN fetch, simulate CDN failure (network block) to confirm fallback + toast, check styling/responsiveness. |

---

### Code Snippets (final merged)

#### `src/utils/cdnHubClient.js`
```js
const REPO = 'AXENTX/Costinel';
const BRANCH = 'main';
const HUB_DATA_PATH = 'knowledge/hubs/top-hub.json';

async function fetchWithTimeout(url, opts = {}) {
  const { timeout = 8000, ...rest } = opts;
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(url, { ...rest, signal: controller.signal });
    clearTimeout(id);
    return res;
  } catch (err) {
    clearTimeout(id);
    throw err;
  }
}

async function fetchJsonWithRetry(url, retries = 2, backoff = 600) {
  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetchWithTimeout(url, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      if (i === retries) throw err;
      await new Promise((r) => setTimeout(r, backoff * 2 ** i));
    }
  }
}

export async function fetchTopHubFromCDN() {
  // CDN bypass: no Authorization header required
  const url = `https://huggingface.co/datasets/${REPO}/resolve/main/${HUB_DATA_PATH}`;
  return fetchJsonWithRetry(url);
}
```

#### `src/assets/data/top-hub-fallback.json`
```json
{
  "hub": "MOC",
  "connections": 42,
  "insights": [
    { "tag": "#knowledge-rag", "text": "Review most-connected hub (MOC) before planning tasks." },
    { "tag": "#graph", "text": "High centrality indicates cross-team dependency leverage point." },
    { "tag": "#business-research", "text": "Align roadmap signals with top hub context for faster decisions." }
  ],
  "updatedAt": "2026-04-27T00:00:00.000Z",
  "source": "fallback"
}
```

#### `src/components/CostinelTopHubCard.vue`
```vue
<template>
  <div class="top-hub-card">
    <div class="card-header">
      <span class="icon">🔗</span>
      <h3>Top Hub Signal</h3>
      <span v-if="loading" class="loading" title="Loading…">⟳</span>
    </div>

    <div v-if="error && !data" class="error" role="alert">
      Unable to load live signal. Showing cached data.
    </div>

    <div v-if="data" class="card-body">
      <div class="hub-name">{{ data.hub }}</div>
      <div class="hub-meta">Connections: {{ data.connections }}</div>
      <ul class="insights" aria-label="Top insights">
        <li v-for="(insight, i) in data.insights" :key="i" class="insight">
          <strong>{{ insight.tag }}</strong> {{ insight.text }}
        </li>
      </ul>
      <div class="card-footer">
        <small>Updated: {{ formatDate(data.updatedAt) }}</small>
        <small v-if="data.source"> · {{ data.source }}</small>
      </div>
    </div>
  </div>
</template>

<script>
import { fetchTopHubFromCDN } from '@/utils/cdnHubClient';
import fallback from '@/assets/data/top-hub-fallback.json';

export default {
  name: 'CostinelTopHubCard',
  data() {
    return {
      data: null,
      loading: false,
      error: null
    };
  },
  async mounted() {
    this.loading = true;
    this.error = null;
    try {
      this.data = await fetchTopHubFromCDN();
    } catch (err) {
      this.error = err;
      this.data = fallback;
      // optional: surface toast/notification here
    } finally {
      this.loading = false;
    }
  },
  methods: {
    formatDate(iso) {
      try {
        return new Date(iso).toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          year: 'numeric'
        });
      } catch {
        return iso;
      }
    }
  }
};
</script>

<style scoped>
.top-hub-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  min-width: 260px;
}
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.card-header h3 {
  font-size: 16px;
  margin: 0;
}
.loading {
  margin-left: auto;
  font-size: 14px;
  opacity: 0.6;
}
.error {
  color: #b91c1c;
  font-size: 13px;
  margin-bottom: 8px;
}
.hub-name {
  font-size: 20px;
  font-weight: 700;
}
.hub-meta {
  color: #6b7280;
  font-size: 14px;
  margin-bottom: 8px;
}
.insights {
  list-style: none;
  padding: 0;
  margin: 8px 0 0 0;
  font-size: 13px;
  color: #111827;
}
.insight {
  padding: 4px 0;
}
.card-footer {
  margin-top: 10px;
  font-size: 11px;
  color: #9ca3af;
  display: flex;
  gap: 4px;
}
</style>
```

#### `src/views/Dashboard.vue` (mount point example)
```vue
<template>
  <div class="dashboard
