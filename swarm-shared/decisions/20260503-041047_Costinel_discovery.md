# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Core principle**: Zero runtime HF API calls. CDN-first, build-time baked data with runtime CDN fetch and robust fallback. Combines Candidate 1’s concrete code with Candidate 2’s schema and resilience.

### 1) Architecture (30s)
- **Build step** (CI/CD or local): `scripts/bake-top-hub.js` → reads local `knowledge-rag/index.json` (or last-known data), writes `public/data/top-hub.json` using Candidate 2’s contract.
- **Runtime**: Frontend loads `https://huggingface.co/datasets/axentx/costinel/resolve/main/public/data/top-hub.json` (CDN) with 3s timeout + localStorage stale-while-revalidate fallback.
- **UI**: Non-blocking slide-in card in sidebar/header; skeleton while loading; manual refresh button; graceful hide on total failure.
- **Failure modes**: CDN 404/timeout → localStorage → hide panel (no errors shown to user).

### 2) Data contract (public/data/top-hub.json)
```json
{
  "hub": "MOC",
  "score": 0.94,
  "label": "Most-connected hub",
  "insight": "High cross-team dependency density; prioritize governance guardrails for MOC-linked resources.",
  "related": [
    { "slug": "ri-coverage", "label": "RI Coverage" },
    { "slug": "anomaly-detection", "label": "Anomaly Detection" }
  ],
  "generatedAt": "2026-05-03T04:10:00.000Z",
  "ttl": 86400
}
```

### 3) Implementation Steps (timed, ~90min total)

#### A) Add bake script (10min)
Create `scripts/bake-top-hub.js` (Node, no external deps):
```js
#!/usr/bin/env node
// scripts/bake-top-hub.js
// Usage: node scripts/bake-top-hub.js
// Produces: public/data/top-hub.json
const fs = require('fs');
const path = require('path');

function readLocalHub() {
  // Prefer local knowledge-rag index if present; otherwise fallback static
  try {
    const indexPath = path.resolve('knowledge-rag', 'index.json');
    if (fs.existsSync(indexPath)) {
      const idx = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
      // pick most-connected node by edges count
      const top = Object.values(idx.nodes || {})
        .sort((a, b) => ((b.edges || []).length) - ((a.edges || []).length))[0];
      if (top) {
        return {
          hub: top.id || 'MOC',
          score: ((top.edges || []).length > 0) ? 0.8 + Math.min(0.2, ((top.edges || []).length * 0.02)) : 0.5,
          label: top.label || top.id || 'MOC',
          insight: `High cross-team dependency density; prioritize governance guardrails for ${top.label || top.id || 'MOC'}-linked resources.`,
          related: (top.related || []).slice(0, 4).map((r) => (typeof r === 'string' ? { slug: r, label: r } : { slug: r.slug || r.id, label: r.label || r.id })),
          generatedAt: new Date().toISOString(),
          ttl: 86400
        };
      }
    }
  } catch (e) {
    // noop
  }
  // fallback default
  return {
    hub: 'MOC',
    score: 0.5,
    label: 'MOC',
    insight: 'No local index available; using fallback hub.',
    related: [],
    generatedAt: new Date().toISOString(),
    ttl: 86400,
    note: 'fallback'
  };
}

function main() {
  const outDir = path.resolve('public', 'data');
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, 'top-hub.json');
  const payload = readLocalHub();
  fs.writeFileSync(outPath, JSON.stringify(payload, null, 2), 'utf8');
  console.log('Baked top-hub:', payload);
}

if (require.main === module) main();
```
Make executable: `chmod +x scripts/bake-top-hub.js`

#### B) Add to build pipeline (2min)
Update package.json scripts:
```json
"scripts": {
  "prebuild": "node scripts/bake-top-hub.js",
  "build": "vite build"
}
```
(If using other bundler, run bake before build step in CI.)

#### C) Runtime CDN fetcher with fallback (15min)
Create `src/lib/cdn.ts` (or `.js`):
```ts
// src/lib/cdn.ts
const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel/resolve/main/public/data/top-hub.json';
const LOCAL_KEY = 'costinel:top-hub';
const TIMEOUT_MS = 3000;

export interface TopHub {
  hub: string;
  score: number;
  label: string;
  insight: string;
  related: Array<{ slug: string; label: string }>;
  generatedAt: string;
  ttl: number;
  note?: string;
}

export async function fetchTopHubCDN(): Promise<TopHub> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(CDN_URL, { signal: controller.signal, cache: 'no-store' });
    clearTimeout(id);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as TopHub;
    try { localStorage.setItem(LOCAL_KEY, JSON.stringify({ data: json, ts: Date.now() })); } catch (e) {}
    return json;
  } catch (err) {
    clearTimeout(id);
    // fallback to localStorage (stale-while-revalidate)
    try {
      const raw = localStorage.getItem(LOCAL_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.data) return parsed.data as TopHub;
      }
    } catch (e) {}
    // final fallback
    return {
      hub: 'MOC',
      score: 0.5,
      label: 'MOC',
      insight: 'Unavailable',
      related: [],
      generatedAt: new Date().toISOString(),
      ttl: 86400,
      note: 'fallback'
    };
  }
}
```

#### D) UI Component (25min)
Create `src/components/TopHubSignalPanel.vue` (Vue 3 example; adapt to React/Svelte as needed):
```vue
<template>
  <div class="panel" :class="{ hidden: !hub && !loading && !error }">
    <div v-if="loading" class="label">Top hub</div>
    <div v-else-if="error && !hub" class="label">Top hub</div>
    <div v-else class="label">{{ hub?.label || hub?.hub }}</div>

    <div v-if="loading" class="skeleton"></div>
    <div v-else-if="hub" class="value">{{ hub.label }}</div>

    <div v-if="hub" class="meta">
      Score: {{ hub.score }}
      <br />Updated {{ formatDate(hub.generatedAt) }}
      <span v-if="hub.note"> ({{ hub.note }})</span>
    </div>

    <div v-if="loading" class="meta">Loading...</div>
    <div v-else-if="error && !hub" class="error-msg">Unavailable</div>

    <div v-if="!loading" class="refresh" @click="load">Refresh</div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { fetchTopHubCDN, type TopHub } from '$lib/cdn';

const hub = ref<TopHub | null>(null);
