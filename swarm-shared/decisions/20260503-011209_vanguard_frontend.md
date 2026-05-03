# vanguard / frontend

### Diagnosis (merged)
- Repeated authenticated `list_repo_tree` (or equivalent `/api/`) calls on every page load burn HF API quota and cause 429s.  
- No persisted `(repo, dateFolder)` manifest on the client, so every load repeats enumeration and file listing.  
- Data fetches use authenticated `/api/` paths instead of public CDN URLs, creating avoidable rate-limit pressure.  
- No request deduplication for in-flight manifest/data fetches → thundering herd when multiple components request the same `(repo, dateFolder)`.  
- No graceful fallback when HF returns 429 (e.g., switch to CDN-only mode or stale cache).  
- Missing robust, quota-safe persistence (localStorage with try/catch) and optional server-side seeding.

---

### Proposed change (merged)
Create a single, reusable HF client that:
1. Persists `(repo, dateFolder)` manifests in `localStorage` (with try/catch).  
2. Uses public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for file/data fetches.  
3. Deduplicates in-flight manifest requests.  
4. Falls back to cached/stale manifest on 429 or network failure.  
5. Is consumed by your route/component layer (SvelteKit `+page.server.ts`/`+page.ts` or equivalent) to seed once per `(repo, dateFolder)` and pass data down.

Files:
- `/opt/axentx/vanguard/src/lib/hf-client.ts` (new)  
- `/opt/axentx/vanguard/src/routes/+page.server.ts` (or loader) — seed manifest server-side once per request.  
- `/opt/axentx/vanguard/src/routes/+page.ts` (or component) — hydrate client-side and fetch files via CDN.  
- `/opt/axentx/vanguard/src/lib/stores.ts` (optional) — shared state for reactivity.

---

### Implementation (merged + concrete)

#### src/lib/hf-client.ts
```ts
// hf-client.ts
// Lightweight HF client: manifest persistence + CDN fetches + 429 fallback + dedup
const HF_REPO = import.meta.env.PUBLIC_HF_REPO || 'your-org/vanguard-data';
const MANIFEST_KEY = 'hf-manifest-v2';
const CDN_ROOT = 'https://huggingface.co/datasets';

export interface FileManifest {
  repo: string;
  dateFolder: string; // e.g. "2026-04-29"
  files: string[];    // relative paths within dateFolder
  etag?: string;
  ts: number;
}

// localStorage helpers (safe)
function getStoredManifests(): Record<string, FileManifest> {
  try {
    return JSON.parse(localStorage.getItem(MANIFEST_KEY) || '{}');
  } catch {
    return {};
  }
}

function storeManifest(m: FileManifest) {
  const all = getStoredManifests();
  all[`${m.repo}:${m.dateFolder}`] = m;
  try {
    localStorage.setItem(MANIFEST_KEY, JSON.stringify(all));
  } catch {
    // ignore quota/incognito
  }
}

// deduplicate in-flight manifest requests
const inFlight = new Map<string, Promise<FileManifest>>();

async function fetchManifestFromAPI(repo: string, dateFolder: string): Promise<FileManifest> {
  // server-side or authenticated endpoint to list tree; keep this single call
  const res = await fetch(`/api/list_repo_tree?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(dateFolder)}`, {
    credentials: 'same-origin'
  });
  if (!res.ok) throw new Error(`list_repo_tree failed: ${res.status}`);
  const tree: string[] = await res.json(); // expect array of paths
  const manifest: FileManifest = { repo, dateFolder, files: tree, ts: Date.now() };
  storeManifest(manifest);
  return manifest;
}

export async function getManifest(repo: string, dateFolder: string, { forceRefresh = false } = {}): Promise<FileManifest> {
  const key = `${repo}:${dateFolder}`;

  // 1) in-flight dedup
  if (inFlight.has(key)) return inFlight.get(key)!;

  // 2) cached (stale-while-revalidate pattern)
  const cached = getStoredManifests()[key];
  if (cached && !forceRefresh) {
    // async refresh in background without blocking
    const p = fetchManifestFromAPI(repo, dateFolder)
      .then((fresh) => {
        inFlight.delete(key);
        return fresh;
      })
      .catch(() => cached); // ignore background failures
    inFlight.set(key, Promise.resolve(cached));
    // kick refresh but return cached immediately
    p.catch(() => {});
    return cached;
  }

  // 3) fetch fresh
  const p = fetchManifestFromAPI(repo, dateFolder)
    .finally(() => inFlight.delete(key));
  inFlight.set(key, p);
  return p;
}

// CDN fetch helpers (public, no auth)
export function buildCdnUrl(repo: string, filePath: string): string {
  return `${CDN_ROOT}/${encodeURIComponent(repo)}/resolve/main/${filePath}`;
}

export async function fetchFileJson(repo: string, filePath: string): Promise<any> {
  const url = buildCdnUrl(repo, filePath);
  const res = await fetch(url, { cache: 'force-cache' });
  if (!res.ok) {
    // If CDN fails (rare), try API as last resort (may hit rate limits)
    const apiFallback = `/api/proxy?url=${encodeURIComponent(url)}`;
    const r2 = await fetch(apiFallback, { credentials: 'same-origin' });
    if (!r2.ok) throw new Error(`CDN+API fetch failed for ${filePath}: ${r2.status}`);
    return r2.json();
  }
  return res.json();
}

// Bulk prefetch with simple concurrency limit
export async function prefetchManifests(
  specs: Array<{ repo: string; dateFolder: string }>,
  concurrency = 4
): Promise<FileManifest[]> {
  const results: FileManifest[] = [];
  const queue = [...specs];
  const workers = Array.from({ length: concurrency }, async () => {
    while (queue.length) {
      const spec = queue.shift()!;
      try {
        const m = await getManifest(spec.repo, spec.dateFolder);
        results.push(m);
      } catch {
        // continue on individual failure
      }
    }
  });
  await Promise.all(workers);
  return results;
}
```

#### src/routes/+page.server.ts (example for SvelteKit)
```ts
// +page.server.ts
import { getManifest } from '$lib/hf-client';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ url }) => {
  const repo = url.searchParams.get('repo') || 'your-org/vanguard-data';
  const dateFolder = url.searchParams.get('date') || '2026-04-29';

  // Single server-side manifest fetch per request (reduces client 429 risk)
  let manifest = null;
  let manifestError = null;
  try {
    manifest = await getManifest(repo, dateFolder);
  } catch (e) {
    manifestError = String(e);
    // try to serve stale from client cache later
  }

  return {
    props: {
      repo,
      dateFolder,
      manifest,
      manifestError
    }
  };
};
```

#### src/routes/+page.ts (client hydration)
```ts
// +page.ts
import { getManifest, fetchFileJson } from '$lib/hf-client';
import type { PageData } from './$types';

export let data: PageData;

let files: any[] = [];
let loading = false;

async function loadFiles() {
  if (!data.manifest) return;
  loading = true;
  try {
    // Fetch each file via CDN (or batch if your files index references a single summary)
    const results = await Promise.all(
      data.manifest.files.slice(0, 20).map((f) =>
        fetchFileJson(data.repo, f).catch(() => null) // tolerate individual failures
      )
    );
    files = results.filter(Boolean);
  } finally
