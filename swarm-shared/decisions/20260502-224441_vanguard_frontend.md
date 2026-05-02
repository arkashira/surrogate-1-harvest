# vanguard / frontend

## Final synthesized solution

**Core diagnosis (merged, prioritized)**
- Dataset detail pages trigger HF API calls (or lack metadata) → 429s and slow loads.
- No CDN-only path for file manifests; no persisted cache; no graceful fallback.
- Dataset detail UI is empty or missing; no previews, counts, or retry behavior.
- Repeated navigation re-fetches same metadata; no offline-first layer.

**Chosen approach**
- Use **sessionStorage + localStorage** (simpler, sufficient per-device) with short TTL and stale-while-revalidate behavior.
- Add a **CDN-only manifest loader** that never calls HF APIs from the browser.
- Build a **dataset detail page** that renders file/folder summary, file table, sizes, last-updated, and graceful fallback UI with retry.
- Keep indexedDB optional; start with storage + CDN to reduce complexity and avoid service-worker scope issues.

---

## 1. Manifest cache + CDN loader (single source)

File: `src/lib/datasetManifest.js`

```js
// src/lib/datasetManifest.js
const CACHE_TTL_MS = 5 * 60 * 1000; // 5 min freshness
const STALE_TTL_MS = 60 * 60 * 1000; // 1 h max stale fallback

function cacheKey(owner, repo, folder = '') {
  return `vanguard:dataset-manifest:${owner}:${repo}:${folder}`;
}

function now() {
  return Date.now();
}

function isFresh(cached) {
  return cached && cached.ts && (now() - cached.ts) < CACHE_TTL_MS;
}

function isStaleUsable(cached) {
  return cached && cached.ts && (now() - cached.ts) < STALE_TTL_MS;
}

function normalize(raw) {
  const files = Array.isArray(raw.files) ? raw.files : [];
  const folders = Array.isArray(raw.folders) ? raw.folders : [];
  const sizes = Array.isArray(raw.sizes) ? raw.sizes : [];
  const generatedAt = raw.generatedAt || new Date().toISOString();
  return { files, folders, sizes, generatedAt };
}

export async function getDatasetManifest(owner, repo, folder = '') {
  if (!owner || !repo) return { files: [], folders: [], sizes: [], generatedAt: new Date().toISOString() };

  const key = cacheKey(owner, repo, folder);
  let cached = null;
  try {
    const raw = sessionStorage.getItem(key) || localStorage.getItem(key);
    cached = raw ? JSON.parse(raw) : null;
  } catch {
    cached = null;
  }

  if (isFresh(cached)) {
    return normalize(cached);
  }

  const base = `https://huggingface.co/datasets/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/resolve/main`;
  const folderPath = folder ? folder.replace(/^\/+/, '').replace(/\/+$/, '') : '';
  const manifestPath = folderPath ? `${folderPath}/file-list.json` : 'file-list.json';
  const url = `${base}/${manifestPath}`;

  try {
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
    const data = await res.json();
    const normalized = normalize({ ...data, generatedAt: data.generatedAt || new Date().toISOString() });

    const toStore = { ...normalized, ts: now() };
    try {
      localStorage.setItem(key, JSON.stringify(toStore));
      sessionStorage.setItem(key, JSON.stringify(toStore));
    } catch {
      try { sessionStorage.setItem(key, JSON.stringify(toStore)); } catch {}
    }
    return normalized;
  } catch (err) {
    // stale-while-revalidate: return stale if usable
    if (isStaleUsable(cached)) return normalize(cached);
    // otherwise graceful empty
    return { files: [], folders: [], sizes: [], generatedAt: new Date().toISOString() };
  }
}

export function getDatasetFileURL(owner, repo, filePath) {
  return `https://huggingface.co/datasets/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/resolve/main/${filePath.replace(/^\/+/, '')}`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes === 0) return '0 B';
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  return `${(bytes / (1024 ** i)).toFixed(1)} ${units[i]}`;
}

export function summarizeManifest(manifest) {
  const fileCount = manifest.files ? manifest.files.length : 0;
  const folderCount = manifest.folders ? manifest.folders.length : 0;
  let totalSize = 0;
  if (Array.isArray(manifest.sizes)) {
    totalSize = manifest.sizes.reduce((sum, s) => sum + (Number.isFinite(s) && s > 0 ? s : 0), 0);
  }
  return {
    fileCount,
    folderCount,
    totalSize,
    totalSizeFormatted: formatBytes(totalSize),
    generatedAt: manifest.generatedAt || ''
  };
}
```

---

## 2. Dataset detail page (SvelteKit example)

File: `src/routes/DatasetDetail.svelte`

```svelte
<script>
  import { onMount } from 'svelte';
  import { getDatasetManifest, getDatasetFileURL, summarizeManifest } from '$lib/datasetManifest.js';

  export let params = {};
  const [owner, repo] = (params.id || '').split('/');

  let manifest = { files: [], folders: [], sizes: [], generatedAt: '' };
  let summary = { fileCount: 0, folderCount: 0, totalSizeFormatted: '', generatedAt: '' };
  let loading = true;
  let error = null;
  let retryCount = 0;

  async function load() {
    loading = true;
    error = null;
    try {
      manifest = await getDatasetManifest(owner, repo);
      summary = summarizeManifest(manifest);
    } catch (err) {
      error = err && err.message ? err.message : 'Failed to load dataset manifest.';
    } finally {
      loading = false;
    }
  }

  async function handleRetry() {
    retryCount++;
    // clear cache for this key to force fresh fetch
    const key = `vanguard:dataset-manifest:${owner}:${repo}:`;
    try { localStorage.removeItem(key); sessionStorage.removeItem(key); } catch {}
    await load();
  }

  onMount(() => {
    if (owner && repo) load();
    else {
      loading = false;
      error = 'Invalid dataset identifier.';
    }
  });
</script>

{#if loading}
  <div class="state">
    <p>Loading dataset manifest…</p>
  </div>
{:else if error}
  <div class="state error">
    <p>⚠️ {error}</p>
    <p class="hint">This may be due to a missing file-list.json or network issue.</p>
    <button on:click={handleRetry}>Retry</button>
    <p class="small">Retry attempts: {retryCount}</p>
  </div>
{:else}
  <div class="dataset-detail">
    <header>
      <h2>{owner}/{repo}</h2>
      <p class="meta">
        Updated: {summary.generatedAt || '—'} |
        {summary.fileCount} file(s) |
        {summary.folderCount} folder(s) |
        {summary.totalSizeFormatted || 'size unknown'}
      </p>
    </header>

    {#if manifest.folders.length || manifest.files.length}
      <section class="folders">
        <h3>Folders</h3>
        {#if manifest.folders.length}
          <ul>
            {#each manifest.folders as folder}
              <li>📁 {folder}</li>
            {/each}
          </ul>
        {:else}
          <p class="muted">No folders listed.</p>
        {/if}
      </section>

      <
