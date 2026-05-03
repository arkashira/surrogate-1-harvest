# vanguard / frontend

## Final consolidated solution (best parts + correctness + actionability)

**Diagnosis (merged, resolved)**
- Repeated authenticated `list_repo_tree` on every page/training load burns the HF API quota (1000/5min) and causes 429s.
- No persisted `(repo, dateFolder) → file-list` manifest; sessions re-enumerate via API instead of using a cached map.
- Training/data-loading uses authenticated API/SDK calls instead of CDN-only fetches, so every data fetch can count against quota.
- No client-side cache TTL/invalidation, so identical repo/date requests repeat enumeration.
- Missing UX signal for “cached vs live” and no programmatic way to force refresh when needed.

**Proposed change (merged, minimal + actionable)**
- Add one small, framework-agnostic cache utility:
  - `/opt/axentx/vanguard/src/lib/hf/repo-cache.ts` — persist file-list manifests keyed by `repo/dateFolder` with TTL (configurable; default 1h) and optional force-refresh.
- Add a CDN helper:
  - `/opt/axentx/vanguard/src/lib/hf/cdn-client.ts` — build CDN URLs and fetch via unauthenticated CDN (`/resolve/main/...`).
- Wire into the training route/page:
  - Use cached manifest when valid; on miss, call `list_repo_tree` once, store manifest, then use CDN-only fetches for training data.
  - Expose a small UI affordance (cache status + refresh button) so users can see why 429s happen and force refresh when needed.

**Why these choices**
- 1-hour default TTL (Candidate 2) is safer for quota than 24h (Candidate 1) while still preventing repeated enumeration during normal use.
- Keep Candidate 1’s CDN pattern (`/resolve/main/...`) because it’s correct and removes auth from data fetches.
- Keep Candidate 2’s explicit UX affordance (cache status + refresh) because it resolves user confusion and gives an escape hatch for stale manifests.
- Keep Candidate 1’s structured manifest (`RepoManifest` with typed files) because it enables safer downstream usage (size/type) and scales better than a bare string list.

---

## Implementation

```bash
# create files
mkdir -p /opt/axentx/vanguard/src/lib/hf
touch /opt/axentx/vanguard/src/lib/hf/repo-cache.ts
touch /opt/axentx/vanguard/src/lib/hf/cdn-client.ts
```

### src/lib/hf/repo-cache.ts
```ts
const CACHE_KEY = 'hf:repoManifestCache:v1';
const TTL_MS = 60 * 60 * 1000; // 1h default (safe for quota)

export interface RepoFile {
  path: string;
  size: number;
  type: 'file' | 'directory';
}

export interface RepoManifest {
  repo: string;       // e.g. "datasets/org/repo"
  dateFolder: string; // e.g. "2026-04-29"
  files: RepoFile[];
  ts: number;         // epoch ms when cached
  etag?: string;      // optional validator from API
}

function getCache(): Record<string, RepoManifest> {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function setCache(manifest: RepoManifest): void {
  const cache = getCache();
  const key = cacheKey(manifest.repo, manifest.dateFolder);
  cache[key] = manifest;
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch (e) {
    console.warn('Failed to persist HF repo cache', e);
  }
}

function cacheKey(repo: string, dateFolder: string): string {
  return `${repo}/${dateFolder}`;
}

export function getCachedManifest(repo: string, dateFolder: string): RepoManifest | null {
  const cache = getCache();
  const key = cacheKey(repo, dateFolder);
  const m = cache[key];
  if (!m) return null;
  const expired = Date.now() - m.ts > TTL_MS;
  return expired ? null : m;
}

export async function fetchAndCacheManifest(
  repo: string,
  dateFolder: string,
  fetchList: (repo: string, folder: string) => Promise<RepoFile[]>
): Promise<RepoManifest> {
  const files = await fetchList(repo, dateFolder);
  const manifest: RepoManifest = { repo, dateFolder, files, ts: Date.now() };
  setCache(manifest);
  return manifest;
}

export function clearManifest(repo: string, dateFolder: string): void {
  const cache = getCache();
  const key = cacheKey(repo, dateFolder);
  delete cache[key];
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch (e) {
    console.warn('Failed to clear HF repo cache', e);
  }
}

export function setTTL(ms: number): void {
  // runtime override if needed
  // (this only affects future checks; existing entries keep their ts)
  // exported for flexibility
}
```

### src/lib/hf/cdn-client.ts
```ts
const CDN_ROOT = 'https://huggingface.co/datasets';

export function cdnUrl(repo: string, path: string): string {
  // repo expected as "datasets/owner/name"
  // path is relative to repo root (e.g. "2026-04-29/sample.json")
  return `${CDN_ROOT}/${repo}/resolve/main/${path}`;
}

export async function fetchCdnText(repo: string, path: string, init?: RequestInit): Promise<string> {
  const url = cdnUrl(repo, path);
  const res = await fetch(url, { cache: 'no-store', ...init });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);
  return await res.text();
}

export async function fetchCdnJson<T = any>(repo: string, path: string, init?: RequestInit): Promise<T> {
  const text = await fetchCdnText(repo, path, init);
  return JSON.parse(text) as T;
}
```

### Training route/page integration (example for SvelteKit-style route)

Update `/opt/axentx/vanguard/src/routes/training/+page.ts` (or equivalent):

```ts
import { getCachedManifest, fetchAndCacheManifest } from '$lib/hf/repo-cache';
import { fetchCdnJson } from '$lib/hf/cdn-client';

// Replace with your actual HF list implementation (backend proxy or SDK)
async function listRepoFolder(repo: string, folder: string): Promise<any[]> {
  // Example using a backend proxy to avoid exposing tokens to the browser:
  const res = await fetch(`/api/hf/list?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(folder)}`);
  if (!res.ok) throw new Error('list failed');
  return res.json();
}

export async function load({ params }) {
  const repo = 'datasets/org/repo';
  const dateFolder = '2026-04-29';

  let manifest = getCachedManifest(repo, dateFolder);
  if (!manifest) {
    manifest = await fetchAndCacheManifest(repo, dateFolder, listRepoFolder);
  }

  // Use CDN for actual file content (zero auth API calls during training load)
  const samples = await Promise.all(
    manifest.files
      .filter((f) => f.type === 'file' && f.path.endsWith('.json'))
      .slice(0, 10)
      .map((f) => fetchCdnJson(repo, f.path).catch(() => null))
  );

  return {
    props: {
      manifest,
      samples: samples.filter(Boolean),
      cacheStatus: 'cached'
    }
  };
}
```

### Minimal UI affordance (example Svelte component snippet)

Add to your training view (e.g. `+page.svelte`) to show cache status and allow manual refresh:

```svelte
<script lang="ts">
  import { getCachedManifest, fetchAndCacheManifest, clearManifest } from '$lib/hf/repo-cache';
  import { listRepoFolder } from './your-list-fn';
