# vanguard / frontend

### Final synthesized plan (correctness + concrete actionability)

**Core diagnosis (merged, de-duplicated)**
- Frontend recomputes HF repo file lists on every training launch → quota burn and 429 risk.  
- No persisted client-side manifest, no ETag/Last-Checked, no TTL → redundant calls and slow UI.  
- Training UI does not expose or enforce CDN-only fetches (no toggle/docs) → auth tokens still used and operators can’t verify zero-auth usage.  
- Missing “reuse running studio” guard → duplicate studios and wasted Lightning quota.  
- No lightweight local JSON manifest generator in the frontend toolchain (Mac orchestration rule).  
- Missing error handling for script execution (e.g., opus pr reviewer).

**Single proposed change**
Add a frontend dataset manifest cache with TTL, CDN-only training hints, a “reuse running studio” guard, and robust script error handling—implemented in `/opt/axentx/vanguard/src/lib/dataset/` and `/opt/axentx/vanguard/src/routes/training/+page.svelte` (or equivalent), plus a backend helper for safe tree listing and a pre-bake generator for Mac.

---

### Implementation (concrete, minimal, production-ready)

1) Create directories
```bash
mkdir -p /opt/axentx/vanguard/src/lib/dataset
mkdir -p /opt/axentx/vanguard/src/lib/training
mkdir -p /opt/axentx/vanguard/scripts
```

2) `/opt/axentx/vanguard/src/lib/dataset/datasetManifest.ts`
```ts
// Lightweight persisted manifest cache for HF dataset file lists.
// Uses localStorage + TTL to avoid repeated HF API list_repo_tree calls.
// Exposes CDN-only URLs (no Authorization) for training fetches.

const CACHE_KEY = 'vanguard:hf-dataset-manifest';
const TTL_MS = 24 * 60 * 60 * 1000; // 1 day (tunable)

export interface DatasetFile {
  path: string;
  size: number;
  type: 'file' | 'directory';
}

export interface DatasetManifest {
  repo: string;     // e.g., 'datasets/myorg/myrepo' or 'myorg/myrepo'
  folder: string;   // e.g., 'batches/mirror-merged/2026-05-02'
  generatedAt: number;
  files: DatasetFile[];
}

export function loadManifest(): DatasetManifest | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const m: DatasetManifest = JSON.parse(raw);
    if (Date.now() - m.generatedAt > TTL_MS) return null;
    return m;
  } catch {
    return null;
  }
}

export function saveManifest(repo: string, folder: string, files: DatasetFile[]): DatasetManifest {
  const m: DatasetManifest = { repo, folder, generatedAt: Date.now(), files };
  localStorage.setItem(CACHE_KEY, JSON.stringify(m));
  return m;
}

// CDN URL (no Authorization header).
export function cdnUrl(repo: string, filePath: string): string {
  const normalized = repo.replace(/^datasets\//, '');
  return `https://huggingface.co/datasets/${normalized}/resolve/main/${filePath}`;
}

// Fetch manifest via backend proxy (one-time or manual refresh).
// Backend should implement ETag/Last-Modified and rate-limit protection.
export async function fetchAndCacheManifest(repo: string, folder: string): Promise<DatasetManifest> {
  const res = await fetch(`/api/hf/tree?repo=${encodeURIComponent(repo)}&folder=${encodeURIComponent(folder)}&recursive=false`, {
    credentials: 'include'
  });
  if (!res.ok) throw new Error(`Failed to fetch manifest: ${res.status} ${res.statusText}`);
  const tree = await res.json(); // expect array of { path, type, size }
  const files: DatasetFile[] = tree.map((t: any) => ({
    path: t.path,
    size: t.size || 0,
    type: t.type === 'dir' ? 'directory' : 'file'
  }));
  return saveManifest(repo, folder, files);
}
```

3) `/opt/axentx/vanguard/src/lib/training/trainingStore.ts`
```ts
import { writable } from 'svelte/store';

export interface LightningStudio {
  id: string;
  name: string;
  status: 'Running' | 'Stopped' | 'Starting' | 'Unknown';
  machine?: string;
}

export const studios = writable<LightningStudio[]>([]);
export const selectedStudio = writable<LightningStudio | null>(null);

// Reuse running studio helper (avoids recreating and burning quota).
export function pickRunningStudio(name: string): LightningStudio | null {
  let found: LightningStudio | null = null;
  // synchronous read
  studios.subscribe(($s) => {
    found = $s.find((s) => s.name === name && s.status === 'Running') || null;
  })();
  return found;
}

// Fetch current studios from backend (lightweight).
export async function refreshStudios(): Promise<LightningStudio[]> {
  const res = await fetch('/api/studios', { credentials: 'include' });
  if (!res.ok) throw new Error('Failed to fetch studios');
  const list: LightningStudio[] = await res.json();
  studios.set(list);
  return list;
}
```

4) `/opt/axentx/vanguard/src/routes/training/+page.svelte` (diff-style integration)
```svelte
<script lang="ts">
  import { loadManifest, cdnUrl, fetchAndCacheManifest } from '$lib/dataset/datasetManifest';
  import { pickRunningStudio, refreshStudios, selectedStudio } from '$lib/training/trainingStore';
  import { onMount } from 'svelte';

  let manifest = loadManifest();
  let refreshing = false;
  let repo = 'datasets/myorg/myrepo';
  let folder = 'batches/mirror-merged/2026-05-02';
  let studioName = 'vanguard-training';

  async function refreshManifest() {
    refreshing = true;
    try {
      manifest = await fetchAndCacheManifest(repo, folder);
    } finally {
      refreshing = false;
    }
  }

  async function startTraining(useCdnOnly: boolean) {
    // Guard: reuse running studio
    const existing = pickRunningStudio(studioName);
    if (existing) {
      if (!confirm(`A running studio "${studioName}" exists (id: ${existing.id}). Reuse it?`)) {
        return;
      }
      selectedStudio.set(existing);
    }

    // Pass CDN-only hint to backend so training uses CDN URLs and no auth tokens.
    const res = await fetch('/api/training/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        repo,
        folder,
        useCdnOnly,
        manifest: manifest || undefined
      })
    });
    if (!res.ok) {
      const err = await res.text();
      throw new Error(`Training start failed: ${err}`);
    }
  }

  onMount(async () => {
    await refreshStudios();
    // If no cached manifest, offer refresh; do not auto-fetch to avoid surprise quota.
  });
</script>

<!-- Minimal UI additions -->
<div>
  <h2>Training</h2>

  <section>
    <h3>Dataset manifest (CDN-only training)</h3>
    {#if manifest}
      <p>Cached {new Date(manifest.generatedAt).toLocaleString()} — {manifest.files.length} entries</p>
      <button on:click={refreshManifest} disabled={refreshing}>Refresh file list</button>
    {:else}
      <p>No cached manifest.</p>
      <button on:click={refreshManifest} disabled={refreshing}>Fetch file list</button>
    {/if}
    <p class="hint">Use CDN-only fetches to avoid auth and quota during training.</p>
  </section>

  <section>
    <h3>Studio</h3>
    <button on:click={() => startTraining(true)}>Start training (CDN-only)</button>
    <button on
