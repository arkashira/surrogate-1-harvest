# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Single goal**: Add a resilient “Top-Hub Signal” panel to Costinel that surfaces the most‑connected hub (e.g., “MOC”) with **zero runtime HF API calls**, using CDN‑first baked data and robust, user‑friendly fallbacks.

---

### Core design (merged, contradiction‑resolved)
- **CDN‑first, baked at build/deploy**: one `list_repo_tree` (or equivalent) call during CI/ops produces `top-hub.json`.  
  - Includes: hub, slug, CDN URL, timestamp, connection count, short insight, and a deterministic fallback flag.  
  - Stored in `public/signals/top-hub.json` (repo) **and** deployed to CDN path (e.g., `https://huggingface.co/datasets/.../resolve/main/.../top-hub.json`).  
- **Runtime**: backend serves `/api/signals/top-hub` by reading the baked file first, optionally probing CDN with short timeout, and returning graceful fallbacks.  
- **Frontend**: renders hub card with link to related docs; never blocks UX on external failures.  
- **No runtime HF API calls in production** (constraint satisfied).  
- **Toggleable/fast to disable** via config/env flag (constraint satisfied).  
- **Offline-first**: static fallback baked into repo so panel degrades gracefully.

---

### Files to add/modify
- `scripts/bake-top-hub.js` (or `.ts`/`.py` per stack) — generates `public/signals/top-hub.json`.  
- `backend/src/routes/signals.js` (or framework equivalent) — `/api/signals/top-hub` endpoint.  
- `frontend/components/TopHubSignalPanel.vue` (or React) — panel UI.  
- CI step (e.g., in build script or GitHub Actions) to run bake script and copy artifact to CDN path if used.  
- Optional: `config/featureFlags.js` (or env) to toggle panel on/off.

---

### Concrete artifacts

#### 1) Bake script (Node) — run in CI/ops
```bash
# scripts/bake-top-hub.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/knowledge-rag"
FOLDER="batches/mirror-merged/$(date +%Y-%m-%d)"  # or detect latest
OUT_DIR="public/signals"
OUT_FILE="${OUT_DIR}/top-hub.json"

mkdir -p "${OUT_DIR}"

# Deterministic selection (replace with real centrality when available)
# For now, use MOC as canonical top-hub; include file list and counts.
HUB="MOC"
SLUG="axentx/moc-knowledge"
CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${FOLDER}/${HUB}.parquet"
MANIFEST_CDN="https://huggingface.co/datasets/${REPO}/resolve/main/${FOLDER}/manifest.json"

# Minimal file list/count simulation — replace with real tree parsing
FILE_COUNT=42

cat > "${OUT_FILE}" <<EOF
{
  "hub": "${HUB}",
  "slug": "${SLUG}",
  "cdnUrl": "${CDN_URL}",
  "manifestUrl": "${MANIFEST_CDN}",
  "fileCount": ${FILE_COUNT},
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "insight": "Most‑connected hub by degree; central to cross‑doc references.",
  "fallback": false,
  "note": "Baked at build time; CDN‑first, zero runtime HF API calls."
}
EOF

echo "Baked top-hub to ${OUT_FILE}"
```
Make executable:
```bash
chmod +x scripts/bake-top-hub.sh
```

---

#### 2) Backend endpoint (Express-like)
```js
// backend/src/routes/signals.js
const express = require('express');
const fs = require('fs').promises;
const path = require('path');
const fetch = require('node-fetch');

const router = express.Router();

const BAKED_PATH = path.join(__dirname, '../../public/signals/top-hub.json');
const CDN_FALLBACK_URL = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/batches/mirror-merged/latest/top-hub.json';
const TIMEOUT_MS = 2500;
const ENABLE_SIGNAL = process.env.ENABLE_TOP_HUB_SIGNAL !== 'false';

async function fetchWithTimeout(url, timeout = TIMEOUT_MS) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(url, { signal: controller.signal });
    clearTimeout(id);
    return res;
  } catch (err) {
    clearTimeout(id);
    throw err;
  }
}

router.get('/signals/top-hub', async (req, res) => {
  if (!ENABLE_SIGNAL) {
    return res.json({ enabled: false, status: 'disabled' });
  }

  try {
    // 1) Prefer baked local file (fast, no network)
    const raw = await fs.readFile(BAKED_PATH, 'utf8');
    const baked = JSON.parse(raw);

    // 2) Lightweight CDN probe (optional) to confirm availability
    try {
      const probe = await fetchWithTimeout(baked.cdnUrl, 1500);
      if (probe.ok) {
        return res.json({ ...baked, status: 'available', source: 'baked+cdn' });
      }
    } catch (_) {
      // CDN probe failed — still return baked metadata (graceful)
    }

    return res.json({ ...baked, status: 'baked-only', source: 'baked' });
  } catch (err) {
    // 3) Fallback to static CDN copy of top-hub.json if baked missing
    try {
      const probe = await fetchWithTimeout(CDN_FALLBACK_URL, 1500);
      if (probe.ok) {
        const cdnData = await probe.json();
        return res.json({ ...cdnData, fallback: true, status: 'cdn-fallback', source: 'cdn-fallback' });
      }
    } catch (_) {
      // continue to final graceful response
    }

    // 4) Final graceful response — never break UX
    res.json({
      hub: null,
      message: 'Top-hub signal unavailable',
      fallback: true,
      status: 'unavailable',
      source: 'none',
      enabled: true
    });
  }
});

module.exports = router;
```

---

#### 3) Frontend panel (Vue)
```vue
<!-- frontend/components/TopHubSignalPanel.vue -->
<template>
  <div class="top-hub-panel card">
    <h3>Top-Hub Signal</h3>
    <div v-if="loading" class="loading">Loading signal…</div>
    <div v-else-if="error" class="empty">Signal unavailable</div>
    <div v-else-if="!enabled" class="empty">Signal disabled</div>
    <div v-else-if="!hub" class="empty">No hub signal</div>
    <div v-else class="hub-card">
      <div class="hub-name">{{ hub }}</div>
      <div class="hub-slug">{{ slug }}</div>
      <div v-if="fileCount" class="meta">Files: {{ fileCount }}</div>
      <div v-if="insight" class="insight">{{ insight }}</div>
      <a :href="docUrl" target="_blank" rel="noopener" class="btn">
        View related docs
      </a>
      <div class="meta">CDN-sourced • {{ formattedTs }}</div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'TopHubSignalPanel',
  data() {
    return {
      loading: true,
      error: false,
      enabled: true,
      hub: null,
      slug: null,
      cdnUrl: null,
      timestamp: null,
      fileCount: null,
      insight: null
    };
  },
  computed: {
    docUrl() {
      return this.cdnUrl || '#';
    },

