# vanguard / frontend

## Final Synthesized Solution

**Scope (frontend only):**  
- `src/lib/dataset-api.ts` (or `datasets.ts`) — unified API utilities  
- `src/routes/(app)/datasets/+page.svelte` (or equivalent route) — route usage  
- `static/manifests/` — build-time generated manifests  
- `scripts/generate-manifest.ts` — Mac/CI script to emit manifests  

---

### 1. Diagnosis (merged + resolved)

- **Authenticated `list_repo_tree` on every page load** → burns HF API quota (1000/5min) and causes 429s.  
  **Resolution:** Eliminate runtime authenticated tree calls for known date-folders; use build-time manifests.
- **Data downloads via authenticated `/api/` paths** → unnecessary auth and stricter limits.  
  **Resolution:** Use public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) for all downloads.
- **No persisted `(repo, dateFolder)` manifest** → repeated expensive listings.  
  **Resolution:** Generate and commit/bundle `static/manifests/{repoSlug}-{dateFolder}.json` from a single Mac/CI HF API call.
- **No client-side caching/dedup for lists or fetches** → redundant calls and thundering-herd on UI interactions.  
  **Resolution:** Add request deduplication, stale-while-revalidate for manifests, and 429-aware retry with exponential backoff.
- **Hardcoded repo/date params in UI** → brittle and prevents manifest reuse.  
  **Resolution:** Accept `repo` and `dateFolder` as explicit parameters; route-level loader uses them to select manifest + CDN root.

---

### 2. Implementation

#### `src/lib/dataset-api.ts`

```ts
// Unified dataset utilities
// - Prefers local static manifests (zero HF API calls at runtime)
// - Falls back to authenticated HF API only when manifest missing
// - Uses public CDN URLs for downloads
// - Deduplicates in-flight requests and retries 429s with backoff

const CDN_ROOT = 'https://huggingface.co/datasets';
const API_ROOT = 'https://huggingface.co/api';

export type FileEntry = {
  path: string;
  size?: number;
  type?: 'file' | 'directory';
};

const inFlight = new Map<string, Promise<any>>();

function dedupe<T>(key: string, fn: () => Promise<T>): Promise<T> {
  if (inFlight.has(key)) return inFlight.get(key) as Promise<T>;
  const p = fn().finally(() => inFlight.delete(key));
  inFlight.set(key, p);
  return p;
}

async function retryFetch(url: string, init: RequestInit & { retries?: number } = {}): Promise<Response> {
  const { retries = 3, ...rest } = init;
  let lastErr: Error | undefined;

  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetch(url, rest);

      // 429: wait and retry
      if (res.status === 429) {
        const retryAfter = Number(res.headers.get('retry-after')) || 60 * (i + 1);
        await new Promise((r) => setTimeout(r, retryAfter * 1000));
        continue;
      }

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res;
    } catch (err) {
      lastErr = err as Error;
      if (i < retries) await new Promise((r) => setTimeout(r, 1000 * 2 ** i));
    }
  }

  throw lastErr ?? new Error('fetch failed');
}

function manifestCacheKey(repo: string, dateFolder: string) {
  return `manifest:${repo}:${dateFolder}`;
}

export async function getRepoTree(
  repo: string,
  dateFolder: string,
  options?: { skipManifest?: boolean }
): Promise<FileEntry[]> {
  return dedupe(manifestCacheKey(repo, dateFolder), async () => {
    // 1) Try local static manifest (preferred, zero auth/rate-limit cost)
    if (!options?.skipManifest) {
      try {
        const slug = repo.replace(/\//g, '-');
        const manifestRes = await fetch(
          `/manifests/${encodeURIComponent(slug)}-${encodeURIComponent(dateFolder)}.json`,
          { cache: 'force-cache' }
        );
        if (manifestRes.ok) {
          const json = await manifestRes.json();
          // Normalize shape if needed
          return Array.isArray(json) ? json : [];
        }
      } catch {
        // fallback to API
      }
    }

    // 2) Fallback to authenticated HF API (dynamic browsing)
    const url = `${API_ROOT}/datasets/${repo}/tree/${encodeURIComponent(
      dateFolder
    )}?recursive=false`;
    const res = await retryFetch(url);
    const tree = await res.json();
    return Array.isArray(tree) ? tree : [];
  });
}

export function getCdnDownloadUrl(repo: string, filePath: string): string {
  return `${CDN_ROOT}/${repo}/resolve/main/${filePath}`;
}

export async function downloadFile(repo: string, filePath: string): Promise<Blob> {
  const url = getCdnDownloadUrl(repo, filePath);
  const res = await retryFetch(url);
  return res.blob();
}
```

---

#### Route usage example (`src/routes/(app)/datasets/+page.svelte`)

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { getRepoTree, downloadFile, type FileEntry } from '$lib/dataset-api';

  // Make repo/date explicit for manifest reuse and clarity
  export let repo = 'your-org/your-dataset';
  export let dateFolder = '2026-04-29';

  let files: FileEntry[] = [];
  let loading = false;
  let error: string | null = null;

  async function loadFiles() {
    loading = true;
    error = null;
    try {
      const tree = await getRepoTree(repo, dateFolder);
      files = tree.filter((f) => f.type === 'file');
    } catch (err) {
      error = (err as Error).message;
    } finally {
      loading = false;
    }
  }

  async function handleDownload(path: string) {
    try {
      const blob = await downloadFile(repo, path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = path.split('/').pop() || 'file';
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error(err);
    }
  }

  onMount(() => {
    loadFiles();
  });
</script>

<main>
  <h1>Dataset: {repo} — {dateFolder}</h1>

  {#if loading}
    <p>Loading files...</p>
  {:else if error}
    <p class="error">Error: {error}</p>
  {:else if files.length === 0}
    <p>No files found.</p>
  {:else}
    <ul>
      {#each files as f}
        <li>
          <span>{f.path}</span>
          <button on:click={() => handleDownload(f.path)}>Download (CDN)</button>
        </li>
      {/each}
    </ul>
  {/if}
</main>

<style>
  .error {
    color: red;
  }
  ul {
    list-style: none;
    padding: 0;
  }
  li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.25rem 0;
  }
</style>
```

---

#### Build-time manifest generator (`scripts/generate-manifest.ts`)

```ts
#!/usr/bin/env tsx
// Generate static manifests to avoid runtime HF API calls.
// Usage: HF_TOKEN=... node scripts/generate-manifest.ts

import { HfApi } from '@huggingface/hub';

