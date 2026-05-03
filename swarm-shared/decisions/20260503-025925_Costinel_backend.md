# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard (sidebar or top banner area).
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, **top 5 signals** (anomalies/recommendations), and last updated timestamp.
- **CDN-first data strategy**: runtime fetches via `https://huggingface.co/datasets/{repo}/resolve/main/...` (no Authorization header, bypasses HF API rate limits).
- **Graceful degradation**: local stub used if CDN is unreachable; panel never blocks dashboard render.
- **Lightning-aware**: if running in a Lightning Studio context, reuse the running Studio and never block training loops (non-blocking async fetch).

---

### File changes (concrete)

#### 1) Hub metadata + CDN path map
`src/config/hubs.json`
```json
{
  "defaultHub": "MOC",
  "hubs": {
    "MOC": {
      "title": "MOC — Multi-Cloud Optimization Council",
      "description": "Top signals for cross-cloud cost governance, anomalies, and RI coverage opportunities.",
      "repo": "axentx/costinel-hubs",
      "folder": "hubs/moc",
      "cdnBase": "https://huggingface.co/datasets/axentx/costinel-hubs/resolve/main/hubs/moc"
    }
  }
}
```

#### 2) CDN fetcher + local fallback
`src/lib/hub-cdn.ts`
```ts
import hubs from '$config/hubs.json';

const HUB_NAME = import.meta.env.VITE_HUB_NAME || hubs.defaultHub;
const hubMeta = hubs.hubs[HUB_NAME] || Object.values(hubs.hubs)[0];

export interface Signal {
  id: string;
  title: string;
  severity: 'low' | 'medium' | 'high';
}

export interface HubData {
  hub: string;
  title: string;
  description: string;
  signals: Signal[];
  updatedAt: string;
}

function getLocalStub(): HubData {
  return {
    hub: HUB_NAME,
    title: `${HUB_NAME} — Signals unavailable`,
    description: 'Using local stub while CDN is unreachable.',
    signals: [
      { id: 'stub-1', title: 'Cost spike detected', severity: 'high' },
      { id: 'stub-2', title: 'Idle resources found', severity: 'medium' },
      { id: 'stub-3', title: 'Low RI coverage', severity: 'medium' },
      { id: 'stub-4', title: 'Unattached volumes', severity: 'high' },
      { id: 'stub-5', title: 'Weekend idle clusters', severity: 'low' },
    ],
    updatedAt: new Date().toISOString(),
  };
}

export async function fetchHubSignals(): Promise<HubData | null> {
  const url = `${hubMeta.cdnBase}/latest.json`;
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    return await res.json();
  } catch (err) {
    console.warn('[Hub] CDN fetch failed, using local stub', err);
    return getLocalStub();
  }
}
```

#### 3) TopHubSignalPanel component (Svelte)
`src/components/TopHubSignalPanel.svelte`
```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { fetchHubSignals, type HubData } from '$lib/hub-cdn';

  let data: HubData | null = null;
  let loading = true;

  onMount(async () => {
    data = await fetchHubSignals();
    loading = false;
  });
</script>

<div class="top-hub-panel">
  {#if loading}
    <div class="skeleton">Loading signals…</div>
  {:else if data}
    <header class="panel-header">
      <span class="hub-badge">{data.hub}</span>
      <h3>{data.title}</h3>
      <p class="muted">{data.description}</p>
      <p class="updated">Updated {new Date(data.updatedAt).toLocaleString()}</p>
    </header>
    <ul class="signals">
      {#each data.signals as s}
        <li class="signal severity-{s.severity}">
          <span class="dot"></span>
          <span class="title">{s.title}</span>
        </li>
      {/each}
    </ul>
    <a class="details" href={`/hubs/${data.hub}`} target="_blank" rel="noopener">
      View details →
    </a>
  {:else}
    <div class="error">Unable to load hub signals.</div>
  {/if}
</div>

<style>
  .top-hub-panel {
    padding: 12px 16px;
    border-left: 3px solid #3b82f6;
    background: #f8fafc;
    font-size: 13px;
    color: #0f172a;
  }
  .panel-header h3 { margin: 4px 0 2px; font-size: 14px; }
  .muted { margin: 0 0 4px; color: #64748b; }
  .updated { margin: 0 0 8px; font-size: 11px; color: #94a3b8; }
  .hub-badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 4px;
    background: #3b82f6;
    color: #fff;
    font-size: 11px;
    font-weight: 600;
  }
  .signals { margin: 0 0 8px; padding-left: 16px; }
  .signal {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 4px 0;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }
  .severity-high .dot { background: #ef4444; }
  .severity-medium .dot { background: #f59e0b; }
  .severity-low .dot { background: #22c55e; }
  .details { font-size: 12px; color: #3b82f6; text-decoration: none; }
  .details:hover { text-decoration: underline; }
  .skeleton, .error { color: #64748b; font-size: 13px; }
</style>
```

#### 4) Mount panel in dashboard
`src/routes/dashboard/+page.svelte` (or equivalent layout)
```svelte
<script>
  import TopHubSignalPanel from '$components/TopHubSignalPanel.svelte';
</script>

<aside class="dashboard-sidebar">
  <TopHubSignalPanel />
  <!-- rest of sidebar content -->
</aside>
```

#### 5) Environment defaults
`.env`
```
VITE_HUB_NAME=MOC
```

#### 6) (Optional) Build-time hub filelist generator (run once/nightly)
`scripts/build-hub-filelist.js`
```js
// Simple Node script to list available hub JSONs and update src/config/hubs.json
// Run via: node scripts/build-hub-filelist.js
// This keeps CDN paths discoverable without runtime API calls.
const fs = require('fs');
const path = require('path');

// Example stub: in practice, read from a known manifest or repo tree
const manifest = {
  defaultHub: 'MOC',
  hubs: {
    MOC: {
      title: 'MOC — Multi-Cloud Optimization Council',
      description: 'Top signals for cross-cloud cost governance, anomalies, and RI coverage opportunities.',
      repo: 'axentx/costinel-hubs',
      folder: 'hubs/moc',
      cdn
