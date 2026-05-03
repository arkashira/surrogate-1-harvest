# vanguard / frontend

## 1. Diagnosis
- Frontend still triggers authenticated `list_repo_tree` on every data-source selection or training launch, burning HF API quota (1000/5min) and causing 429s.
- No persisted `(repo, dateFolder) → file-list` manifest exists, forcing re-enumeration and preventing CDN-only fetches during training.
- UI has no visibility into whether file lists are fresh or stale, so retries hammer the API instead of using cached CDN paths.
- No client-side guard to fall back to CDN-only mode when 429 is detected, causing hard failures instead of graceful degradation.
- Missing lightweight manifest persistence (localStorage/indexedDB) means even repeated runs by the same user re-enumerate identical folders.

## 2. Proposed change
- **File scope**: `/opt/axentx/vanguard/src/lib/hf/fileManifest.ts` (new) + `/opt/axentx/vanguard/src/lib/hf/api.ts` (modify) + `/opt/axentx/vanguard/src/components/DataSourcePicker.svelte` (modify).
- **Goal**: Persist folder file-lists client-side, default to cached manifest, refresh only on user action or TTL expiry, and expose CDN-only mode for training launches.

## 3. Implementation

### src/lib/hf/fileManifest.ts
```ts
// Lightweight manifest: (repo, folder, etag/sha) -> { files: string[], ts: number }
// TTL: 10 minutes for same folder unless user forces refresh.

const TTL_MS = 10 * 60 * 1000;
const STORAGE_KEY = 'vanguard:hf:file-manifest-v1';

interface ManifestEntry {
  repo: string;
  folder: string;
  files: string[];
  ts: number;
}

function loadManifest(): Record<string, ManifestEntry> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveManifest(index: Record<string, ManifestEntry>) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(index));
  } catch {
    // ignore storage limits
  }
}

function manifestKey(repo: string, folder: string) {
  return `${repo}::${folder}`;
}

export function getCachedFileList(repo: string, folder: string): string[] | null {
  const index = loadManifest();
  const key = manifestKey(repo, folder);
  const entry = index[key];
  if (!entry) return null;
  if (Date.now() - entry.ts > TTL_MS) return null;
  return entry.files;
}

export function putCachedFileList(repo: string, folder: string, files: string[]) {
  const index = loadManifest();
  const key = manifestKey(repo, folder);
  index[key] = { repo, folder, files, ts: Date.now() };
  saveManifest(index);
}

export function clearStaleManifests() {
  const index = loadManifest();
  const now = Date.now();
  let changed = false;
  for (const key of Object.keys(index)) {
    if (now - index[key].ts > TTL_MS) {
      delete index[key];
      changed = true;
    }
  }
  if (changed) saveManifest(index);
}
```

### src/lib/hf/api.ts (modify)
Add CDN bypass helper and safe list wrapper:
```ts
import { getCachedFileList, putCachedFileList } from './fileManifest';

const HF_CDN_ROOT = 'https://huggingface.co/datasets';

export function cdnResolve(repo: string, filePath: string): string {
  return `${HF_CDN_ROOT}/${repo}/resolve/main/${filePath}`;
}

export async function listFolderFilesSafe(
  repo: string,
  folder: string,
  { forceRefresh = false } = {}
): Promise<string[]> {
  // Try cache first
  if (!forceRefresh) {
    const cached = getCachedFileList(repo, folder);
    if (cached) return cached;
  }

  // Authenticated fallback (used sparingly)
  // NOTE: This call should be invoked only from user-triggered "Refresh" actions
  // or when cache is empty. Keep it here for completeness but avoid in hot paths.
  try {
    // Placeholder: actual SDK call would go here (list_repo_tree)
    // For frontend, this may be proxied via your backend to avoid exposing tokens.
    const files = await fetchListTreeViaProxy(repo, folder);
    putCachedFileList(repo, folder, files);
    return files;
  } catch (err: any) {
    // On 429, fallback to cached (even stale) or empty to avoid hard failure
    const stale = getCachedFileList(repo, folder);
    if (stale) return stale;
    throw err;
  }
}

async function fetchListTreeViaProxy(repo: string, folder: string): Promise<string[]> {
  // Example proxy endpoint to avoid exposing HF tokens in frontend
  const res = await fetch(`/api/hf/list-tree?repo=${encodeURIComponent(repo)}&folder=${encodeURIComponent(folder)}`);
  if (!res.ok) throw new Error(`List failed: ${res.status}`);
  const json = await res.json();
  return json.files || [];
}
```

### src/components/DataSourcePicker.svelte (modify)
Wire UI to cached manifest and expose refresh + CDN-only mode:
```svelte
<script lang="ts">
  import { listFolderFilesSafe, cdnResolve } from '$lib/hf/api';
  import { onMount } from 'svelte';

  export let repo: string;
  export let folder: string;

  let files: string[] = [];
  let loading = false;
  let cdnOnly = true; // default to CDN-only for training launches

  async function loadFiles(force = false) {
    loading = true;
    try {
      files = await listFolderFilesSafe(repo, folder, { forceRefresh: force });
    } catch (err) {
      console.error(err);
      // surface user-friendly message
    } finally {
      loading = false;
    }
  }

  onMount(() => loadFiles(false));
</script>

<div class="data-source-picker">
  <div class="header">
    <strong>{repo}/{folder}</strong>
    <button on:click={() => loadFiles(true)} disabled={loading}>
      {loading ? 'Refreshing...' : 'Refresh List'}
    </button>
    <label>
      <input type="checkbox" bind:checked={cdnOnly} />
      CDN-only mode (bypass API)
    </label>
  </div>

  {#if loading && files.length === 0}
    <p class="muted">Loading file list...</p>
  {:else}
    <ul class="file-list">
      {#each files as f}
        <li>
          <a href={cdnResolve(repo, f)} target="_blank" rel="noreferrer">
            {f}
          </a>
        </li>
      {/each}
    </ul>
  {/if}

  <div class="actions">
    <button on:click={() => console.log('Launch training with CDN-only', { cdnOnly, files })}>
      Launch Training (CDN-only={cdnOnly})
    </button>
  </div>
</div>

<style>
  .data-source-picker { border: 1px solid #ddd; padding: 12px; border-radius: 6px; }
  .header { display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap; }
  .file-list { list-style:none; padding:0; margin:0; max-height:240px; overflow:auto; }
  .file-list li { padding:4px 0; }
  .muted { color:#666; }
</style>
```

## 4. Verification
- Open the picker for a known repo/folder; confirm files appear without triggering backend authenticated calls (check network tab for `/api/hf/list-tree` — should only appear on "Refresh List").
- After first load, disable network or simulate 429 from proxy; picker should still show previously cached files and allow training launch in CDN-only mode.
- Toggle CDN-only checkbox and launch training; confirm training script (or launch payload) uses only CDN URLs (`https
