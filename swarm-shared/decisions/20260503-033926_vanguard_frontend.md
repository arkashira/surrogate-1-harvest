# vanguard / frontend

## Final Synthesis (Corrected + Actionable)

**Core diagnosis (merged, de-duplicated):**
- The frontend still triggers runtime HF API calls (`list_repo_tree`, `load_dataset`, or equivalent) during dataset selection/preview, causing 429s and non-reproducible runs.
- No deterministic, content-addressed manifest (date/slug-keyed) is available to the frontend; jobs re-enumerate files each run.
- Missing CDN-first strategy: public parquet files at `https://huggingface.co/datasets/{repo}/resolve/main/{path}` are not used as the primary, zero-auth fetch path.
- No local caching of manifests or shard lists in the frontend, so sessions repeat expensive metadata calls.
- No resilient retry/backoff or graceful degradation when CDN fetches fail or HF API limits are approached.

**Guiding principles for resolution:**
- Correctness: eliminate all runtime HF API metadata calls from the frontend; use only CDN URLs and a pinned manifest.
- Actionability: implement frontend-only first (no backend changes) while keeping a clear path for backend/manifest generation.
- Resilience: add memoization, retry/backoff, and clear UX indicators.
- Portability: support SvelteKit (Vanguard) as the primary UI; provide equivalent guidance for React if needed.

---

## 1. File: dataset manifest loader (SvelteKit)

Path: `/opt/axentx/vanguard/src/lib/datasetManifest.ts`

```ts
// /opt/axentx/vanguard/src/lib/datasetManifest.ts
const MANIFEST_PATH = '/manifests/mirror-merged/latest-manifest.json';
const HF_DATASETS_CDN = 'https://huggingface.co/datasets';
const LOCALSTORAGE_KEY = 'axentx:datasetManifest:v1';
const MEMORY_TTL_MS = 5 * 60 * 1000; // 5m
const RETRY_DELAY_BASE_MS = 800;
const MAX_RETRIES = 3;

export interface ShardEntry {
  repo: string;   // e.g. "org/surrogate-1"
  date: string;   // e.g. "2026-04-29"
  slug: string;   // content-addressed slug
  path: string;   // relative path to parquet in repo
  url: string;    // CDN bypass URL
}

export interface DatasetManifest {
  generatedAt: string;
  repo: string;
  entries: ShardEntry[];
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

export class DatasetManifestLoader {
  private memoryCache: DatasetManifest | null = null;
  private lastFetch = 0;

  constructor(private manifestPath = MANIFEST_PATH) {}

  private persistToLocal(manifest: DatasetManifest) {
    try {
      localStorage.setItem(LOCALSTORAGE_KEY, JSON.stringify(manifest));
    } catch {
      // ignore storage quota / private mode errors
    }
  }

  private loadFromLocal(): DatasetManifest | null {
    try {
      const raw = localStorage.getItem(LOCALSTORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as DatasetManifest;
      // Basic shape validation
      if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.entries)) return null;
      return parsed;
    } catch {
      return null;
    }
  }

  private async fetchWithRetry(force = false): Promise<DatasetManifest> {
    let lastErr: Error | null = null;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const res = await fetch(this.manifestPath, { cache: force ? 'reload' : 'no-store' });
        if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
        const manifest: DatasetManifest = await res.json();

        // Normalize + ensure CDN URLs
        manifest.entries = manifest.entries.map((e) => ({
          ...e,
          url: `${HF_DATASETS_CDN}/${e.repo}/resolve/main/${e.path}`,
        }));

        return manifest;
      } catch (err: any) {
        lastErr = err;
        if (attempt < MAX_RETRIES) {
          const delay = RETRY_DELAY_BASE_MS * 2 ** attempt;
          await sleep(delay + Math.random() * 200);
        }
      }
    }
    throw lastErr ?? new Error('Failed to fetch manifest after retries');
  }

  async load(force = false): Promise<DatasetManifest> {
    const now = Date.now();

    // 1) Memory cache hit
    if (!force && this.memoryCache && now - this.lastFetch < MEMORY_TTL_MS) {
      return this.memoryCache;
    }

    // 2) Try network with retries
    try {
      const manifest = await this.fetchWithRetry(force);
      this.memoryCache = manifest;
      this.lastFetch = now;
      this.persistToLocal(manifest);
      return manifest;
    } catch (err) {
      // 3) Fallback to localStorage
      const local = this.loadFromLocal();
      if (local) {
        // Ensure CDN URLs on fallback entries too
        local.entries = local.entries.map((e) => ({
          ...e,
          url: `${HF_DATASETS_CDN}/${e.repo}/resolve/main/${e.path}`,
        }));
        this.memoryCache = local;
        return local;
      }
      throw err;
    }
  }

  getShardUrls(manifest: DatasetManifest): string[] {
    return manifest.entries.map((e) => e.url);
  }
}
```

---

## 2. Route/Page: datasets (SvelteKit)

Path: `/opt/axentx/vanguard/src/routes/(app)/datasets/+page.svelte`

```svelte
<!-- /opt/axentx/vanguard/src/routes/(app)/datasets/+page.svelte -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { DatasetManifestLoader } from '$lib/datasetManifest';

  let manifest: DatasetManifest | null = null;
  let loading = false;
  let error: string | null = null;
  let usingFallback = false;
  const loader = new DatasetManifestLoader();

  async function loadManifest(force = false) {
    loading = true;
    error = null;
    try {
      const start = Date.now();
      manifest = await loader.load(force);
      // If load took 0 retries and came from localStorage quickly, it's a fallback
      // We detect fallback by checking if generatedAt is older than MEMORY_TTL (heuristic)
      if (manifest) {
        const gen = new Date(manifest.generatedAt).getTime();
        usingFallback = Date.now() - gen > 5 * 60 * 1000;
      }
    } catch (e: any) {
      error = e.message;
      manifest = null;
    } finally {
      loading = false;
    }
  }

  onMount(() => loadManifest(false));
</script>

<section class="p-4">
  <div class="flex items-center justify-between mb-4">
    <div>
      <h1 class="text-xl font-semibold">Dataset shards (CDN-first)</h1>
      {#if manifest}
        <p class="text-xs text-gray-400">
          Generated {new Date(manifest.generatedAt).toLocaleString()}
          {#if usingFallback}
            <span class="ml-1 italic">(using local cache)</span>
          {/if}
        </p>
      {/if}
    </div>

    <div class="flex items-center gap-2">
      <button
        on:click={() => loadManifest(true)}
        disabled={loading}
        class="btn btn-sm btn-outline"
        title="Re-fetch manifest from server"
      >
        {loading ? 'Refreshing...' : 'Refresh manifest'}
      </button>
    </div>
  </div>

  {#if loading}
    <p class="text-sm text-gray-500">Loading manifest...</p>
  {/if}

  {#if error}
    <div class="text-sm text-red-600 mb-2">
      {error}
      <p class="text-xs text-gray-500 mt-1">
        Tip: If the server
