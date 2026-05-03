# Costinel / quality

# Final Implementation Plan — Top Hub Signal Panel (CDN-first, frontend-only)

**Scope**: Add a lightweight, resilient Top Hub signal card to the Costinel dashboard that surfaces the most-connected hub (e.g., “MOC”) and related contextual insights without backend changes.  
**Effort**: ~60–90 minutes (frontend only).  
**Mechanics**:
- Orchestration (Mac/CI) calls `list_repo_tree` for one date folder and writes `public/data/hub-file-list.json` into the repo.
- Frontend loads that index, then fetches each file via **HF CDN** (`resolve/main/...`) with no Authorization header.
- Parse only `{prompt,response}` at render time; ignore extra schema fields.
- Deterministic repo selection for future writes (hash-slug → 1 of 5 siblings) when/if we push enriched results.
- Defensive UI: skeleton → error → stale-data fallback; no retries in UI (fail-fast, log to console).
- Graceful fallbacks for 429/404; optional client-side cache (localStorage, 5-minute TTL) to reduce CDN load.
- Card links to full insights view (modal or route) with attribution in filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`).

---

## File changes

### 1) Orchestration script (run from Mac/CI)

`scripts/sync-top-hub-list.sh`
```bash
#!/usr/bin/env bash
# Usage: bash scripts/sync-top-hub-list.sh <date>
# Example: bash scripts/sync-top-hub-list.sh 2026-04-27
# Requires: HF_TOKEN in env, jq
set -euo pipefail

REPO="axentx/top-hub-insights"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="public/data/hub-file-list.json"

mkdir -p "$(dirname "$OUT")"

# List one date folder (non-recursive) to avoid pagination/rate-limit.
# Uses HF API (subject to 429). Run sparingly (e.g., once/day in CI).
echo "Listing ${REPO}/${DATE}..."
TREES=$(curl -sSf \
  -H "Authorization: Bearer ${HF_TOKEN}" \
  "https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE}&recursive=false" \
  | jq '[.[] | select(.type=="file") | .path]')

# Save relative paths under the date folder
echo "$TREES" | jq --arg d "$DATE" '[.[] | select(startswith($d + "/"))]' > "$OUT"
echo "Saved $(echo "$TREES" | jq length) files to $OUT"
```

Make executable:
```bash
chmod +x scripts/sync-top-hub-list.sh
```

Crontab (optional, run once/day after rate-limit window):
```cron
SHELL=/bin/bash
0 6 * * * cd /opt/axentx/Costinel && bash scripts/sync-top-hub-list.sh >> logs/sync-hub.log 2>&1
```

---

### 2) Frontend: TopHubSignalPanel.vue

`src/components/dashboard/TopHubSignalPanel.vue`
```vue
<template>
  <section class="top-hub-panel card">
    <header class="card-header">
      <h3 class="title">Top Hub Signal</h3>
      <span class="subtitle">Knowledge-RAG insights (CDN-first)</span>
    </header>

    <div class="card-body">
      <!-- Loading -->
      <div v-if="loading" class="hub-skeletons">
        <div v-for="n in 3" :key="n" class="hub-skeleton"></div>
      </div>

      <!-- Error -->
      <div v-else-if="error" class="hub-error">
        <p>Unable to load Top Hub signals.</p>
        <small>{{ error }}</small>
        <button v-if="hasStale" @click="useStale" class="btn-link">Use cached data</button>
      </div>

      <!-- Content -->
      <div v-else-if="items.length" class="hub-list">
        <article v-for="item in items" :key="item.slug" class="hub-item">
          <h4 class="hub-title">{{ item.title || item.slug }}</h4>
          <p class="hub-prompt">{{ item.prompt }}</p>
          <blockquote class="hub-response">{{ item.response }}</blockquote>
          <footer class="hub-meta">
            <span class="hub-badge">hub:{{ item.hub || 'MOC' }}</span>
            <time :datetime="item.date">{{ item.date }}</time>
            <a :href="item.parquetHref" target="_blank" rel="noopener" class="hub-link">View parquet</a>
          </footer>
        </article>
      </div>

      <!-- Empty -->
      <div v-else class="hub-empty">
        No signals available for today.
      </div>
    </div>
  </section>
</template>

<script>
const HUB_REPO = 'axentx/top-hub-insights';
const CDN_ROOT = `https://huggingface.co/datasets/${HUB_REPO}/resolve/main`;
const CACHE_KEY = 'costinel_hub_cache';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

export default {
  name: 'TopHubSignalPanel',
  data() {
    return {
      loading: true,
      error: null,
      items: [],
      hasStale: false
    };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      this.error = null;

      try {
        // Try cache first
        const cached = this.readCache();
        if (cached) {
          this.items = cached;
          this.hasStale = true;
        }

        // 1) Load file list (produced by sync script)
        const listRes = await fetch('/data/hub-file-list.json', { cache: 'no-store' });
        if (!listRes.ok) throw new Error('File list unavailable');
        const paths = await listRes.json();
        if (!Array.isArray(paths) || paths.length === 0) {
          this.items = [];
          this.writeCache([]);
          return;
        }

        // 2) Fetch each via CDN (no auth) and parse {prompt,response}
        const items = [];
        for (const path of paths) {
          try {
            const url = `${CDN_ROOT}/${path}`;
            const raw = await fetchWithTimeout(url, 8000);
            const obj = await raw.json();

            // Project only what we need; ignore extra schema fields
            items.push({
              slug: path.split('/').pop().replace(/\.[^/.]+$/, ''),
              date: path.split('/')[0] || '',
              hub: path.split('/')[1] || 'MOC',
              title: obj.title || obj.slug || path,
              prompt: obj.prompt || '',
              response: obj.response || '',
              parquetHref: `batches/mirror-merged/${path.split('/')[0]}/${path.split('/').pop().replace(/\.[^/.]+$/, '')}.parquet`
            });
          } catch (err) {
            console.warn('Failed to load hub file:', path, err);
            // Continue with others (fail-soft)
          }
        }

        // Deterministic ordering: most recent first, then by slug
        items.sort((a, b) => {
          const cmp = b.date.localeCompare(a.date);
          return cmp !== 0 ? cmp : a.slug.localeCompare(b.slug);
        });

        this.items = items;
        this.writeCache(items);
        this.hasStale = false;
      } catch (err) {
        this.error = err.message || String(err);
        console.error(err);
      } finally {
        this.loading = false;
      }
    },

    readCache() {
      try {
        const raw = localStorage.getItem(CACHE_KEY);
        if (!raw) return null;
        const { ts, items } = JSON.parse(raw);
        if (Date.now() - ts > CACHE_TTL_MS) return null;
        return items;
      } catch {
        return null
