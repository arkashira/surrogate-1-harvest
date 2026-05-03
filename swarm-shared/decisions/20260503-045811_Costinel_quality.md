# Costinel / quality

## Final Implementation Plan — Top Hub Signal Panel (CDN-first, frontend-only)

**Scope**: Add a lightweight, resilient Top Hub signal card to the Costinel dashboard.  
**Effort**: ~60–90 minutes (frontend only).  
**Mechanism**: CDN JSON fetch (no auth, no backend) with local fallback, TTL caching, and graceful degradation.

---

### Why this is the highest-value <2h improvement
- Applies **Pattern: top-hub doc insight** — surfaces the most-connected hub (e.g., "MOC") for contextual governance signals.
- Uses **Pattern: CDN Bypass** — fetches public JSON via CDN (`resolve/main/`) to avoid API rate limits and auth complexity.
- Zero backend changes, zero infra, zero secrets — safe to ship and rollback.
- Improves dashboard decision quality with minimal surface area.

---

### CDN payload contract (single source of truth)

Host at:  
`https://huggingface.co/datasets/axentx/Costinel-data/resolve/main/top-hub.json`

```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "score": 0.94,
  "rank": 1,
  "signals": [
    {
      "id": "ri-coverage-gap",
      "title": "RI coverage gap in us-east-1",
      "severity": "high",
      "action": "Review 12-month RI commitment for EC2",
      "context": "37% of running instances are on-demand in RI-eligible families"
    },
    {
      "id": "idle-ebs",
      "title": "Idle EBS volumes detected",
      "severity": "medium",
      "action": "Snapshot & detach unattached volumes",
      "context": "8 unattached gp3 volumes >30 days"
    }
  ],
  "updatedAt": "2026-05-03T04:56:00.000Z",
  "ttl": 3600
}
```

**Rationale**: This schema merges Candidate 1’s rich, multi-signal structure with Candidate 2’s clarity. It supports ranking, scoring, and multiple actionable signals while remaining simple to generate and consume.

---

### File changes

- `src/components/TopHubSignalCard.vue` (new)
- `src/dashboard/Dashboard.vue` (import + mount)
- `public/data/top-hub.json` (local fallback)
- `src/composables/useTopHubSignal.js` (new)

---

### Composable: `useTopHubSignal.js`

Handles CDN fetch, timeout, fallback, localStorage TTL caching, and validation.

```js
// src/composables/useTopHubSignal.js
const CDN_URL =
  'https://huggingface.co/datasets/axentx/Costinel-data/resolve/main/top-hub.json';
const FALLBACK_URL = '/data/top-hub.json';
const CACHE_KEY = 'costinel:top-hub:cached';
const CACHE_TTL_MS = 3600_000;

function getCached() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const item = JSON.parse(raw);
    if (Date.now() - item.ts > CACHE_TTL_MS) return null;
    return item.payload;
  } catch {
    return null;
  }
}

function setCached(payload) {
  try {
    localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ ts: Date.now(), payload })
    );
  } catch {
    // ignore storage errors
  }
}

function isValidPayload(json) {
  return (
    json &&
    typeof json.hub === 'string' &&
    typeof json.label === 'string' &&
    typeof json.score === 'number' &&
    Number.isInteger(json.rank) &&
    Array.isArray(json.signals) &&
    json.signals.every(
      (s) =>
        s &&
        typeof s.id === 'string' &&
        typeof s.title === 'string' &&
        typeof s.severity === 'string' &&
        typeof s.action === 'string' &&
        typeof s.context === 'string'
    )
  );
}

export function useTopHubSignal() {
  const data = ref(getCached() || null);
  const loading = ref(false);
  const error = ref(null);

  async function fetchFrom(url, isFallback = false) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const res = await fetch(url, {
        signal: controller.signal,
        cache: 'no-store'
      });
      clearTimeout(timeout);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (!isValidPayload(json)) {
        throw new Error('Invalid payload schema');
      }
      setCached(json);
      data.value = json;
      error.value = null;
      return json;
    } catch (err) {
      clearTimeout(timeout);
      // If primary CDN fails, try fallback once (but not for abort)
      if (!isFallback && err.name !== 'AbortError') {
        return fetchFrom(FALLBACK_URL, true);
      }
      error.value = err.message || 'Failed to load top-hub signal';
      // keep cached data if available
      return null;
    }
  }

  async function load() {
    // optimistic: show cached immediately, refresh in background
    if (getCached()) data.value = getCached();
    loading.value = true;
    await fetchFrom(CDN_URL);
    loading.value = false;
  }

  return { data, loading, error, load };
}
```

---

### Component: `TopHubSignalCard.vue`

Lightweight card with skeleton, severity badges, accessible markup, and time-ago display.

```vue
<!-- src/components/TopHubSignalCard.vue -->
<template>
  <section class="top-hub-card" aria-labelledby="hub-title">
    <header class="card-header">
      <div class="hub-title-row">
        <h3 id="hub-title" class="hub-title">
          <span class="hub-badge" :class="`hub-badge--${rankClass}`">
            #{{ rank }}
          </span>
          {{ hubLabel }}
        </h3>
        <span class="hub-score" :title="`Signal score ${score}`">
          {{ (score * 100).toFixed(0) }}%
        </span>
      </div>
      <p class="hub-sub">Top hub signal — governance context</p>
      <time class="hub-updated" :datetime="updatedAt">
        Updated {{ updatedTimeAgo }}
      </time>
    </header>

    <div v-if="loading && !data" class="signals-skeleton" aria-hidden="true">
      <div class="sk-row" v-for="n in 2" :key="n"></div>
    </div>

    <ul v-else class="signals-list" role="list">
      <li
        v-for="s in signals"
        :key="s.id"
        class="signal-item"
        :class="`signal-item--${s.severity}`"
      >
        <div class="signal-title">{{ s.title }}</div>
        <div class="signal-meta">
          <span class="signal-severity" :class="`sev-${s.severity}`">
            {{ s.severity }}
          </span>
          <span class="signal-action">{{ s.action }}</span>
        </div>
        <p class="signal-context">{{ s.context }}</p>
      </li>
    </ul>

    <div v-if="error && !data" class="card-error" role="status">
      {{ error }} — showing cached data if available.
    </div>
  </section>
</template>

<script>
import { computed } from 'vue';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import { useTopHubSignal } from '@/composables/useTopHubSignal';

dayjs.extend(relativeTime);

export default {
  name: 'TopHubSignalCard',
  setup() {
    const { data, loading, error, load } =
