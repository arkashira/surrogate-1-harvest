# Costinel / discovery

Below is the **single, merged implementation** that keeps the strongest parts of both proposals, removes contradictions, and is written for immediate, concrete execution.

Decision log (short):
- Use **Vue** (Candidate 2) for consistency with the existing codebase shown (`Dashboard.vue`, `.vue` components).
- Use **CDN-first with local fallback** (both agree). Use Candidate 2’s CDN URL pattern (`cdn.axentx.io`) and Candidate 1’s strong fallback strategy.
- Use **Candidate 2’s data contract** (clean, minimal) but preserve Candidate 1’s `tags` because they’re useful for context.
- Use **Candidate 2’s service wrapper** (timeout/retry) and Candidate 1’s graceful UI states.
- Keep everything **frontend-only, ~60–90 minutes**, no auth, no backend changes.

---

## 1) Create the CDN payload (one-time)

Path (committed to repo):  
`public/signals/top-hub.json`

Content:
```json
{
  "hub": "MOC",
  "title": "Most-connected hub",
  "score": 94,
  "trend": "up",
  "insight": "MOC shows strongest cross-team signal convergence this week. Prioritize governance reviews for workloads touching MOC-linked resources.",
  "updatedAt": "2026-05-03T05:00:00Z",
  "ttl": 3600,
  "tags": ["#knowledge-rag", "#graph", "#hub"],
  "link": "https://axentx.internal/knowledge-rag/hubs/MOC"
}
```

- This file is the **local fallback** and the source you push to the CDN (`cdn.axentx.io/signals/top-hub/latest.json`).  
- To update later: overwrite this file and re-sync to CDN (or automate via CI).

---

## 2) Add tiny CDN service (timeout + retry)

File: `src/services/cdnService.js`
```js
const CDN_URL = 'https://cdn.axentx.io/signals/top-hub/latest.json';
const TIMEOUT = 4000; // ms

function timeout(ms) {
  return new Promise((_, reject) =>
    setTimeout(() => reject(new Error('CDN timeout')), ms)
  );
}

export async function fetchTopHubSignal() {
  // Try CDN with timeout + one retry
  for (const attempt of [1, 2]) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), TIMEOUT);
      const res = await fetch(CDN_URL, { signal: controller.signal, cache: 'no-cache' });
      clearTimeout(timer);
      if (!res.ok) throw new Error(`CDN HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      if (attempt === 2) throw err;
    }
  }
}
```

---

## 3) Create the Vue signal card

File: `src/components/TopHubSignalCard.vue`
```vue
<template>
  <div class="p-4 border rounded bg-white shadow-sm">
    <div v-if="loading" class="text-sm text-gray-500">
      Loading top hub signal…
    </div>

    <div v-else-if="error || !signal" class="text-sm text-amber-700 bg-amber-50 p-3 rounded">
      Signal unavailable.
    </div>

    <div v-else>
      <div class="flex items-start justify-between">
        <div>
          <div class="flex items-center gap-2">
            <span class="font-semibold text-gray-900">Top Hub</span>
            <span class="px-2 py-0.5 rounded bg-blue-100 text-blue-800 text-xs font-medium">
              {{ signal.hub }}
            </span>
          </div>
          <p class="mt-1 text-sm text-gray-600">{{ signal.insight }}</p>
          <div class="mt-2 flex flex-wrap gap-1">
            <span
              v-for="tag in signal.tags"
              :key="tag"
              class="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600"
            >
              {{ tag }}
            </span>
          </div>
        </div>
        <a
          v-if="signal.link"
          :href="signal.link"
          target="_blank"
          rel="noopener noreferrer"
          class="text-xs text-blue-600 hover:underline whitespace-nowrap"
        >
          View hub →
        </a>
      </div>
      <div class="mt-3 text-xs text-gray-400">
        Updated {{ formatDate(signal.updatedAt) }}
      </div>
    </div>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue';
import { fetchTopHubSignal } from '@/services/cdnService';

export default {
  name: 'TopHubSignalCard',
  setup() {
    const signal = ref(null);
    const loading = ref(true);
    const error = ref(null);

    async function load() {
      loading.value = true;
      error.value = null;
      try {
        signal.value = await fetchTopHubSignal();
      } catch (err) {
        // CDN failed: try local fallback
        try {
          const res = await fetch('/signals/top-hub.json', { cache: 'no-cache' });
          if (!res.ok) throw new Error('Local fallback failed');
          signal.value = await res.json();
        } catch (fallbackErr) {
          error.value = fallbackErr.message;
        }
      } finally {
        loading.value = false;
      }
    }

    function formatDate(iso) {
      return new Date(iso).toLocaleString();
    }

    onMounted(load);

    return { signal, loading, error, formatDate };
  },
};
</script>
```

---

## 4) Mount the card on the dashboard

File: `src/views/Dashboard.vue`

Locate the signals row (or main grid) and add:

```vue
<script>
import TopHubSignalCard from '@/components/TopHubSignalCard.vue';

export default {
  components: { TopHubSignalCard },
  // ...existing component options
};
</script>

<template>
  <div>
    <!-- existing header/controls -->

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
      <!-- existing cards -->
      <div class="lg:col-span-1">
        <TopHubSignalCard />
      </div>
    </div>

    <!-- rest of dashboard -->
  </div>
</template>
```

If your layout differs, place the card in a visible, non-intrusive zone (below header or in a right sidebar).

---

## 5) Verify (quick checklist)

1. Start dev server (`npm run dev` or equivalent).
2. Confirm the card appears and shows **MOC** with tags and insight.
3. Test CDN failure by temporarily changing `CDN_URL` to a bad path — it should fall back to `/signals/top-hub.json` without errors.
4. Confirm no console errors and UI remains responsive.
5. Check mobile width to ensure card wraps gracefully.

---

## 6) Optional: tiny deploy/CDN sync helper

File: `scripts/sync-top-hub-cdn.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

SRC="public/signals/top-hub.json"
# Replace with your actual CDN upload (e.g., AWS S3, GitHub Pages, etc.)
# Example for S3:
# aws s3 cp "$SRC" s3://your-cdn-bucket/signals/top-hub/latest.json --acl public-read --cache-control max-age=3600

echo "Local payload ready at $SRC"
echo "Upload to CDN to publish updates."
```

Make executable:
```bash
chmod +x scripts/sync-top-hub-cdn.sh
```

---

## Summary

- **What ships**: A Vue-based Top Hub signal card (frontend-only) that surfaces MOC via CDN JSON with robust local fallback.

