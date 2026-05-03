# vanguard / frontend

## Final synthesized implementation (correct + actionable)

**Core principle**: Fetch the repo tree **once per 24 h** via a minimal authenticated API route, persist a lightweight manifest client-side, and use **public CDN URLs** for all file fetches. Provide an explicit refresh path and graceful degraded mode when the API is throttled.

---

### 1. Manifest store (localStorage-first, robust)

`src/lib/stores/fileManifest.ts`
```ts
// Persisted HF file manifest + CDN URL builder.
// Uses localStorage for simplicity and broad support; falls back to memory.

const MF_KEY = 'vanguard_hf_manifest';
const MF_TTL_MS = 24 * 60 * 60 * 1000; // 24h

export interface HFManifest {
  repo: string;
  dateFolder: string; // e.g. "2026-04-29"
  files: string[];    // filenames (not full paths)
  fetchedAt: number;  // epoch ms
}

function isManifestLike(v: unknown): v is HFManifest {
  return (
    !!v &&
    typeof v === 'object' &&
    'repo' in v &&
    'dateFolder' in v &&
    'files' in v &&
    'fetchedAt' in v &&
    typeof (v as any).repo === 'string' &&
    typeof (v as any).dateFolder === 'string' &&
    Array.isArray((v as any).files) &&
    typeof (v as any).fetchedAt === 'number'
  );
}

export function saveManifest(m: HFManifest): void {
  try {
    localStorage.setItem(MF_KEY, JSON.stringify(m));
  } catch {
    // ignore storage errors (private mode, quota)
  }
}

export function loadManifest(): HFManifest | null {
  try {
    const raw = localStorage.getItem(MF_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!isManifestLike(parsed)) return null;
    if (Date.now() - parsed.fetchedAt > MF_TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function getFileCDNUrl(manifest: HFManifest, file: string): string {
  // Public CDN URL — no Authorization header required.
  return `https://huggingface.co/datasets/${manifest.repo}/resolve/main/${manifest.dateFolder}/${encodeURIComponent(file)}`;
}

export async function refreshManifest(
  repo: string,
  dateFolder: string,
  fetchImpl: typeof fetch
): Promise<HFManifest | null> {
  // Expect server route to proxy list_repo_tree (keeps tokens out of client).
  const res = await fetchImpl(`/api/hf/tree?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(dateFolder)}&recursive=false`);
  if (!res.ok) return null;
  const tree = await res.json(); // { files: string[] }
  const files = Array.isArray(tree.files)
    ? tree.files.filter((f: unknown) => typeof f === 'string' && f.trim()).map((f: string) => f.trim())
    : [];
  const m: HFManifest = { repo, dateFolder, files, fetchedAt: Date.now() };
  saveManifest(m);
  return m;
}
```

---

### 2. Server-side loader (SSR-friendly)

`src/routes/+page.server.ts`
```ts
import { loadManifest, refreshManifest } from '$lib/stores/fileManifest';

const REPO = 'axentx/dataset-mirror';
const DATEFOLDER = '2026-04-29';

export async function load({ fetch }) {
  let manifest = loadManifest();

  // Try to refresh only if no valid manifest exists.
  // In production, move periodic refreshes to a cron/Mac orchestrator and expose static JSON.
  if (!manifest) {
    manifest = await refreshManifest(REPO, DATEFOLDER, fetch);
  }

  return {
    manifest,
    repo: REPO,
    dateFolder: DATEFOLDER
  };
}
```

---

### 3. UI component (Svelte)

`src/routes/+page.svelte`
```svelte
<script lang="ts">
  import { getFileCDNUrl, refreshManifest, loadManifest } from '$lib/stores/fileManifest';

  export let manifest;
  export let repo: string;
  export let dateFolder: string;

  let isRefreshing = false;
  $: files = manifest?.files ?? [];

  async function onRefresh() {
    isRefreshing = true;
    const m = await refreshManifest(repo, dateFolder, fetch);
    if (m) manifest = m;
    isRefreshing = false;
  }
</script>

<section>
  <h2>Dataset files ({files.length})</h2>
  <button on:click={onRefresh} disabled={isRefreshing}>
    {isRefreshing ? 'Refreshing…' : 'Refresh manifest'}
  </button>

  {#if files.length === 0}
    <p>No files available.</p>
  {:else}
    <ul>
      {#each files as file}
        <li>
          <a href={getFileCDNUrl(manifest, file)} target="_blank" rel="noopener">
            {file}
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>
```

---

### 4. Minimal authenticated proxy route (keeps tokens server-side)

`src/routes/api/hf/tree/+server.ts`
```ts
import { list_repo_tree } from '$lib/hf-api';

export async function GET({ url }) {
  const repo = url.searchParams.get('repo');
  const path = url.searchParams.get('path');
  if (!repo || !path) return new Response('Missing repo/path', { status: 400 });

  try {
    // recursive=false to minimize payload and quota use
    const tree = await list_repo_tree(repo, path, false);
    const files = (tree || [])
      .filter((f: any) => f?.type === 'file')
      .map((f: any) => {
        const p = String(f.path || '');
        return p.split('/').pop();
      })
      .filter(Boolean);

    return new Response(JSON.stringify({ files }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (e) {
    console.error('[hf/tree]', e);
    return new Response('Failed to list tree', { status: 502 });
  }
}
```

---

### 5. Verification checklist

- [ ] Load page → file list appears.
- [ ] Inspect links → all point to `https://huggingface.co/datasets/.../resolve/main/...` (CDN, no `/api/` for downloads).
- [ ] DevTools → Application → Local Storage → `vanguard_hf_manifest` exists with correct `repo`, `dateFolder`, `files`, `fetchedAt`.
- [ ] Disable network and reload → page still renders file list from cache (stale-acceptable).
- [ ] Click “Refresh manifest” → localStorage timestamp updates; new file list appears if changed.
- [ ] Network tab → no authenticated calls for file downloads; only `/api/hf/tree` on cache miss and CDN fetches.
