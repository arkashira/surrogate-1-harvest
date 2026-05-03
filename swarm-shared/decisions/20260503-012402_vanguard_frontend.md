# vanguard / frontend

## Final consolidated solution (best of both proposals)

**Core diagnosis (agreed)**
- Authenticated `list_repo_tree` on page load burns HF quota and causes 429s.
- Authenticated `/api/` paths are used for file fetches when public CDN URLs should be used.
- No persisted `(repo, dateFolder)` manifest exists, so every session re-enumerates files.
- No CDN-only download path; previews/downloads cannot scale without quota exhaustion.
- No fallback/caching when HF API is rate-limited.

**Chosen approach**
- Use **TypeScript** (Candidate 2) for type safety and maintainability.
- Keep **localStorage manifest cache** (Candidate 1) for instant client-side availability, with a clear upgrade path to a **static JSON manifest served from CDN** (Candidate 1 recommendation) to eliminate client-side API calls entirely.
- **Never run authenticated HF API calls from the browser on page load**. Provide an explicit, guarded refresh path (server-side preferred).
- All file downloads use **public CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization headers.

---

### 1) Create/replace HF client (TypeScript)

```bash
# Ensure directory exists
mkdir -p /opt/axentx/vanguard/src/lib/data
```

```ts
// /opt/axentx/vanguard/src/lib/data/hf-client.ts
const MANIFEST_KEY = 'vanguard_hf_manifest_v1';
const CDN_ROOT = 'https://huggingface.co/datasets';
const MANIFEST_TTL_MS = 24 * 60 * 60 * 1000; // 24h

export interface HfFileEntry {
  path: string;
  size: number;
}

export function buildCdnUrl(repo: string, path: string): string {
  return `${CDN_ROOT}/${repo}/resolve/main/${path}`;
}

function manifestStorageKey(repo: string, dateFolder: string): string {
  return `${MANIFEST_KEY}:${repo}:${dateFolder}`;
}

export function saveManifest(repo: string, dateFolder: string, entries: HfFileEntry[]): void {
  const key = manifestStorageKey(repo, dateFolder);
  try {
    localStorage.setItem(key, JSON.stringify({ entries, savedAt: Date.now() }));
  } catch (e) {
    // ignore in private mode / storage-full
    console.warn('localStorage unavailable', e);
  }
}

export function loadManifest(repo: string, dateFolder: string): HfFileEntry[] | null {
  const key = manifestStorageKey(repo, dateFolder);
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { entries: HfFileEntry[]; savedAt: number };
    if (Date.now() - parsed.savedAt > MANIFEST_TTL_MS) return null;
    return parsed.entries;
  } catch (e) {
    return null;
  }
}

// Optional: fetch static manifest hosted on CDN (recommended for production)
export async function fetchStaticManifest(repo: string, dateFolder: string): Promise<HfFileEntry[] | null> {
  // Example: host at /static/manifests/{repo}/{dateFolder}.json on your CDN or server
  const staticUrl = `/static/manifests/${encodeURIComponent(repo)}/${encodeURIComponent(dateFolder)}.json`;
  try {
    const res = await fetch(staticUrl, { cache: 'no-cache' });
    if (!res.ok) return null;
    const entries: HfFileEntry[] = await res.json();
    // validate minimal shape
    if (Array.isArray(entries) && entries.every((e) => typeof e.path === 'string' && typeof e.size === 'number')) {
      saveManifest(repo, dateFolder, entries);
      return entries;
    }
    return null;
  } catch (e) {
    return null;
  }
}

// Authenticated tree API — DO NOT CALL THIS ON PAGE LOAD FROM BROWSER.
// Use only via explicit server-side job or guarded manual refresh with token.
export async function fetchManifestOnce(repo: string, dateFolder: string, token: string): Promise<HfFileEntry[]> {
  const url = `https://huggingface.co/api/models/${repo}/tree/${encodeURIComponent(dateFolder)}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`HF tree API failed: ${res.status}`);
  const items: Array<{ type: string; path: string; size: number }> = await res.json();
  const entries: HfFileEntry[] = items
    .filter((i) => i.type === 'file' && i.path.endsWith('.parquet'))
    .map((i) => ({ path: i.path, size: i.size }));
  saveManifest(repo, dateFolder, entries);
  return entries;
}

// Get entries: prefer static manifest -> localStorage -> (optionally) authenticated refresh.
// Default behavior avoids HF API entirely when no token provided and no cache present.
export async function getEntries(
  repo: string,
  dateFolder: string,
  options: { refresh?: boolean; token?: string; allowStatic?: boolean } = {}
): Promise<HfFileEntry[]> {
  const { refresh = false, token, allowStatic = true } = options;

  // 1) Try static manifest first (recommended)
  if (allowStatic) {
    const staticEntries = await fetchStaticManifest(repo, dateFolder);
    if (staticEntries && staticEntries.length) return staticEntries;
  }

  // 2) Try localStorage cache
  const cached = loadManifest(repo, dateFolder);
  if (cached && !refresh) return cached;

  // 3) If refresh requested and token provided, perform authenticated fetch (use sparingly)
  if (refresh && token) {
    return fetchManifestOnce(repo, dateFolder, token);
  }

  // 4) No cache, no token, no refresh -> return empty to avoid quota burn
  return [];
}

// Fetch file via CDN (no auth, no API quota)
export async function fetchFileAsArrayBuffer(repo: string, filePath: string): Promise<ArrayBuffer> {
  const url = buildCdnUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
  return res.arrayBuffer();
}

// Convenience: direct CDN download via anchor (no XHR/fetch required)
export function downloadViaCdn(repo: string, filePath: string, filename?: string): void {
  const url = buildCdnUrl(repo, filePath);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || filePath.split('/').pop() || 'file';
  a.click();
}
```

---

### 2) Update frontend route (Svelte + TypeScript)

```svelte
<!-- /opt/axentx/vanguard/src/routes/+page.svelte -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { getEntries, downloadViaCdn, buildCdnUrl } from '$lib/data/hf-client';

  const repo = 'your-org/your-dataset-repo';
  const dateFolder = 'batches/mirror-merged/2026-04-29';

  let entries: Array<{ path: string; size: number }> = [];
  let loading = false;
  let error: string | null = null;

  async function loadEntries(refresh = false) {
    // Never pass a token by default on page load.
    loading = true;
    error = null;
    try {
      const list = await getEntries(repo, dateFolder, { refresh, token: undefined });
      entries = list;
    } catch (e: any) {
      error = e?.message || String(e);
    } finally {
      loading = false;
    }
  }

  function handleRefresh() {
    // Do not call authenticated API from browser.
    // Recommended: trigger server-side manifest generation (e.g., via POST /api/generate-manifest)
    alert('Refresh disabled in UI to avoid HF API quota burn. Use server-side manifest generation.');
  }

  onMount(() => {
    loadEntries(false);
  });
</script>

<
