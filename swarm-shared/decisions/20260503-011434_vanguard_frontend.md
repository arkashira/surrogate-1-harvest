# vanguard / frontend

### Synthesized Solution

The proposed solution combines the strongest insights from both candidates, resolving contradictions in favor of correctness and concrete actionability.

#### Diagnosis

* No persisted `(repo, dateFolder)` manifest on the client, leading to repeated authenticated `list_repo_tree`/`/api/` calls and burning HF quota.
* Data fetches use authenticated API paths instead of public CDN URLs, resulting in unnecessary auth overhead and tighter rate limits.
* No client-side cache invalidation policy, causing stale manifests to persist indefinitely.
* Repeated enumeration of repo tree on each load wastes bandwidth and increases latency.
* Missing lightweight TTL mechanism to gate refreshes and allow manual refresh.

#### Proposed Change

* **File scope**: Create or modify `src/lib/hf-client.ts` and `src/routes/+page.svelte` (or equivalent page).
* **Add**: A `HFManifestStore` that persists `{ repo, dateFolder, files[], ts }` to `localStorage` with a 24h TTL.
* **Add**: A single authenticated "seed" endpoint call to populate the manifest only when missing/stale.
* **Change**: All file downloads to use public CDN URLs derived from the manifest.
* **Add**: A manual "Refresh file list" button that clears the manifest for that key and re-seeds.

#### Implementation

```typescript
// src/lib/hf-client.ts
import { writable } from 'svelte/store';

const MANIFEST_KEY = 'hf_manifest_v1';
const TTL_MS = 24 * 60 * 60 * 1000; // 24h

export interface HFManifest {
  repo: string;
  dateFolder: string;
  files: string[];
  ts: number;
}

function isFresh(manifest: HFManifest): boolean {
  return Date.now() - manifest.ts < TTL_MS;
}

export async function loadManifest(opts: {
  repo: string;
  dateFolder: string;
  token?: string;
  forceRefresh?: boolean;
}): Promise<HFManifest> {
  const { repo, dateFolder, token, forceRefresh } = opts;

  // Try cached
  if (!forceRefresh) {
    try {
      const raw = localStorage.getItem(MANIFEST_KEY);
      if (raw) {
        const cached: HFManifest = JSON.parse(raw);
        if (cached.repo === repo && cached.dateFolder === dateFolder && isFresh(cached)) {
          return cached;
        }
      }
    } catch (e) {
      // Ignore storage errors
    }
  }

  // Fetch fresh tree (single non-recursive call)
  const url = `https://huggingface.co/api/datasets/${repo}/tree`;
  const params = new URLSearchParams({ path: dateFolder, recursive: 'false' });
  const resp = await fetch(`${url}?${params}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {}
  });

  if (!resp.ok) {
    throw new Error(`HF tree fetch failed: ${resp.status}`);
  }

  const tree = await resp.json();
  const files = (tree as any[])
    .filter((n) => n.type === 'file')
    .map((n) => n.path as string)
    .filter(Boolean);

  const manifest: HFManifest = { repo, dateFolder, files, ts: Date.now() };
  try {
    localStorage.setItem(MANIFEST_KEY, JSON.stringify(manifest));
  } catch (e) {
    // Ignore quota/storage errors
  }
  return manifest;
}

export function getCdnUrl(manifest: HFManifest, file: string): string {
  return `https://huggingface.co/datasets/${manifest.repo}/resolve/main/${manifest.dateFolder}/${encodeURIComponent(file)}`;
}

// Optional: store for reactivity
export const manifestStore = writable<HFManifest | null>(null);
```

```svelte
<!-- src/routes/+page.svelte -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { loadManifest, getCdnUrl, manifestStore } from '$lib/hf-client';

  let files: string[] = [];
  let loading = false;
  let error: string | null = null;

  async function refreshManifest(force = false) {
    loading = true;
    error = null;
    try {
      const m = await loadManifest({
        repo: 'your-org/your-dataset',
        dateFolder: '2026-05-03',
        token: import.meta.env.VITE_HF_TOKEN, // Optional; omit for public repos
        forceRefresh: force
      });
      files = m.files;
      manifestStore.set(m);
    } catch (e: any) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  onMount(() => refreshManifest(false));
</script>

<button on:click={() => refreshManifest(true)} disabled={loading}>
  {loading ? 'Refreshing...' : 'Refresh manifest'}
</button>

{#if error}
  <p class="error">{error}</p>
{/if}

<ul>
  {#each files as f}
    {@const url = manifestStore ? getCdnUrl($manifestStore, f) : '#'}
    <li>
      <a href={url} target="_blank" rel="noopener">{f}</a>
    </li>
  {/each}
</ul>
```

#### Verification

* Load the page and confirm no authenticated `/api/` requests appear in the Network tab for tree listing after the first load (only one on initial or manual refresh).
* Confirm file links point to `https://huggingface.co/datasets/.../resolve/main/...` (CDN) and fetch successfully without Authorization headers.
* Check `localStorage` for `hf_manifest_v1` and verify it contains `{ repo, dateFolder, files[], ts }` and that `ts` is recent.
* Wait >24 h (or modify TTL to 10s for a quick test) and confirm a new tree fetch occurs on the next load.
* Simulate rate-limit stress: with the manifest cached, repeated page reloads should not trigger additional authenticated API calls (verify via Network).
