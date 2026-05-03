# vanguard / frontend

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest on the frontend → repeated HF API enumeration on page loads → quota burn + 429 risk.
- Data fetches likely use authenticated `/api/` paths instead of public CDN URLs → avoidable rate-limit pressure.
- No client-side caching layer (IndexedDB / localStorage) for training file lists → unnecessary network roundtrips and latency.
- Missing fallback when Lightning Studio is idle/stopped → UI can silently stall instead of restarting the target.
- No reuse check for existing Lightning Studio instances → wastes 80hr/mo quota when iterating frontend-launched training jobs.

## 2. Proposed change
File: `/opt/axentx/vanguard/src/lib/data/fileManifest.ts` (new)  
Scope: create a lightweight manifest manager + CDN URL builder and wire it into the training launcher component (`/opt/axentx/vanguard/src/routes/train/+page.svelte` or equivalent).  
Lines: add ~120 lines total (new file + small integration patches).

## 3. Implementation

### 3.1 Create manifest manager
```ts
// src/lib/data/fileManifest.ts
import { writable } from 'svelte/store';

export interface FileEntry {
  path: string;
  size: number;
  sha: string;
  url: string; // CDN-only
}

export interface ManifestKey {
  repo: string;
  dateFolder: string; // e.g. "2026-04-29"
}

type Manifest = Record<string, FileEntry[]>; // JSON-serializable

const STORAGE_KEY = 'vanguard_file_manifest_v1';

function loadManifest(): Manifest {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveManifest(manifest: Manifest) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(manifest));
}

function keyOf(k: ManifestKey): string {
  return `${k.repo}::${k.dateFolder}`;
}

export const manifestStore = writable<Manifest>(loadManifest());

export function getManifest(k: ManifestKey): FileEntry[] | null {
  const m = loadManifest();
  return m[keyOf(k)] || null;
}

export function setManifest(k: ManifestKey, entries: FileEntry[]) {
  const m = loadManifest();
  m[keyOf(k)] = entries;
  saveManifest(m);
  manifestStore.set(m);
}

export function buildCdnUrl(repo: string, path: string): string {
  // Public CDN — no Authorization header required
  return `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;
}
```

### 3.2 Add lightweight API caller (Mac/orchestrator only)
```ts
// src/lib/api/hfManifest.ts
import type { FileEntry } from '$lib/data/fileManifest';
import { buildCdnUrl, getManifest, setManifest } from '$lib/data/fileManifest';

const API_ROOT = 'https://huggingface.co/api';

export async function fetchFolderTree(repo: string, dateFolder: string, token?: string) {
  // Non-recursive: one folder listing only
  const url = `${API_ROOT}/datasets/${repo}/tree?path=${encodeURIComponent(dateFolder)}&recursive=false`;
  const headers: HeadersInit = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(url, { headers });
  if (!res.ok) {
    // If 429, caller should backoff and retry after window clears
    throw new Error(`HF API error ${res.status}`);
  }
  const items: Array<{ path: string; size: number; sha: string; type: 'file' | 'dir' }> = await res.json();
  return items.filter((i) => i.type === 'file');
}

export async function ensureManifest(
  repo: string,
  dateFolder: string,
  token?: string
): Promise<FileEntry[]> {
  const key = { repo, dateFolder };
  const cached = getManifest(key);
  if (cached) return cached;

  const files = await fetchFolderTree(repo, dateFolder, token);
  const entries: FileEntry[] = files.map((f) => ({
    path: f.path,
    size: f.size,
    sha: f.sha,
    url: buildCdnUrl(repo, f.path),
  }));
  setManifest(key, entries);
  return entries;
}
```

### 3.3 Wire into training launcher (example)
```svelte
<!-- src/routes/train/+page.svelte (snippet) -->
<script lang="ts">
  import { ensureManifest } from '$lib/api/hfManifest';
  import { Lightning } from '$lib/api/lightning';
  import { onMount } from 'svelte';

  let status = 'idle';
  let studio = null;

  async function launchTraining() {
    status = 'preparing';
    const repo = 'your-org/surrogate-1';
    const dateFolder = '2026-04-29';

    // 1) Manifest (CDN-only after first fetch)
    const files = await ensureManifest(repo, dateFolder, 'optional-token-if-needed');

    // 2) Reuse running studio if available
    const running = Lightning.listRunningStudios().find((s) => s.name === 'surrogate-train-v1');
    if (running) {
      studio = running;
    } else {
      studio = await Lightning.createStudio({
        name: 'surrogate-train-v1',
        machine: 'L40S', // or fallback to free-tier compatible
      });
    }

    // 3) Pass file list (CDN URLs) to training script — zero HF API calls during load
    await studio.run('train.py', {
      FILE_LIST_JSON: JSON.stringify(files.map((f) => f.url)),
    });

    status = 'running';
  }

  onMount(() => {
    // Optional: preload manifest silently
    ensureManifest('your-org/surrogate-1', '2026-04-29').catch(() => {});
  });
</script>

<button on:click={launchTraining} disabled={status !== 'idle'}>
  {status === 'idle' ? 'Launch Training' : status}
</button>
```

## 4. Verification
1. Open DevTools → Application → Local Storage and confirm `vanguard_file_manifest_v1` appears after first run with correct `repo::dateFolder` key and CDN URLs.
2. Network tab: after first load, no further authenticated `/api/` calls to `tree` for that folder; training script receives CDN URLs only.
3. Toggle to a different date folder and confirm a new manifest entry is created (and reused on refresh).
4. Stop the Lightning Studio manually, then click “Launch Training” again — UI should detect stopped state and restart the target (or show clear error if reuse fails).
5. With an expired/invalid token, ensure `ensureManifest` still succeeds for public repos via CDN fallback (if token omitted or 429, UI should not crash).
