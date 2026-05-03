# vanguard / frontend

### 1. Diagnosis (merged + prioritized)
- Repeated HF API enumeration on every page load (no persisted `(repo, dateFolder)` manifest) → quota burn + 429 risk.  
- Data fetches use authenticated `/api/` paths instead of public CDN → avoidable rate-limit pressure.  
- No client-side caching for file lists/metadata → re-fetch across refreshes/route changes.  
- No graceful fallback when HF API is throttled → UI hard-fails instead of using stale cache + CDN.  
- Potential `pyarrow.CastError` on mixed schemas when using `load_dataset(streaming=True)` on heterogeneous repos (backend/data-pipeline concern).  
- Missing efficient data-loading strategy → slow page loads, poor UX.

### 2. Proposed change (merged + concrete)
- **Scope**: frontend data layer (`src/lib/data/hf-cdn.ts`; create if absent).  
- Add a **manifest fetcher** that:
  - Calls HF API **once per `(repo, dateFolder)`** (non-recursive `list_repo_tree`) and caches result in `localStorage` (TTL ~10–15 min).  
  - Exposes CDN URLs for each file: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.  
  - Uses **no Authorization header for CDN fetches**; only the initial manifest call uses auth (on cache miss).  
  - Falls back to stale cache when API fails/throttled to keep UI functional.  
- Update all dataset file fetches to use CDN URLs from the manifest instead of `/api/` paths.  
- Add lightweight in-memory cache for metadata/files to avoid redundant parsing during session.  
- Backend: ensure homogeneous schema or cast safely when using `load_dataset(streaming=True)`; validate once at ingestion and store canonical schema with manifest.

### 3. Implementation (merged + production-ready)

```ts
// src/lib/data/hf-cdn.ts
const HF_API_BASE = 'https://huggingface.co/api';
const HF_CDN_BASE = 'https://huggingface.co/datasets';

const CACHE_TTL_MS = 15 * 60 * 1000; // 15 min
const MEM_TTL_MS = 5 * 60 * 1000;    // 5 min in-memory cache to reduce localStorage churn

interface RepoFile {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

interface ManifestCache {
  repo: string;
  dateFolder: string;
  files: RepoFile[];
  ts: number;
}

// In-memory cache for fast repeated access during session
const memCache = new Map<string, { files: RepoFile[]; ts: number }>();

function cacheKey(repo: string, dateFolder: string): string {
  return `hf-manifest:${repo}:${dateFolder}`;
}

function isMemValid(key: string): boolean {
  const entry = memCache.get(key);
  return !!entry && Date.now() - entry.ts < MEM_TTL_MS;
}

function isDiskValid(cache: ManifestCache | null): boolean {
  if (!cache) return false;
  return Date.now() - cache.ts < CACHE_TTL_MS;
}

export async function getDatasetManifest(
  repo: string,
  dateFolder: string,
  token?: string
): Promise<RepoFile[]> {
  const key = cacheKey(repo, dateFolder);

  // 1) In-memory fast path
  if (isMemValid(key)) {
    return memCache.get(key)!.files;
  }

  // 2) Disk cache path
  let cached: ManifestCache | null = null;
  try {
    const raw = localStorage.getItem(key);
    cached = raw ? JSON.parse(raw) : null;
  } catch {
    cached = null;
  }

  if (isDiskValid(cached) && cached?.files) {
    memCache.set(key, { files: cached.files, ts: Date.now() });
    return cached.files;
  }

  // 3) API fetch (non-recursive tree for dateFolder)
  const url = `${HF_API_BASE}/repos/datasets/${repo}/tree?path=${encodeURIComponent(
    dateFolder
  )}&recursive=false`;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    });
  } catch (err) {
    // Network failure: fallback to stale cache if available
    if (cached?.files) {
      memCache.set(key, { files: cached.files, ts: Date.now() });
      return cached.files;
    }
    throw new Error(`HF API unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }

  if (!res.ok) {
    // Throttled or server error: fallback to stale cache
    if (cached?.files) {
      memCache.set(key, { files: cached.files, ts: Date.now() });
      return cached.files;
    }
    throw new Error(`HF API failed: ${res.status} ${res.statusText}`);
  }

  const items: RepoFile[] = await res.json();
  const files = items.filter((i) => i.type === 'file') as RepoFile[];

  const entry: ManifestCache = { repo, dateFolder, files, ts: Date.now() };
  try {
    localStorage.setItem(key, JSON.stringify(entry));
  } catch {
    // ignore quota errors
  }
  memCache.set(key, { files, ts: Date.now() });
  return files;
}

export function getCdnUrl(repo: string, filePath: string): string {
  // Normalize double slashes and ensure proper encoding
  const normalized = filePath.replace(/^\/+/, '');
  return `${HF_CDN_BASE}/${repo}/resolve/main/${encodeURI(normalized)}`;
}

export async function getDatasetFileUrls(
  repo: string,
  dateFolder: string,
  token?: string
): Promise<string[]> {
  const files = await getDatasetManifest(repo, dateFolder, token);
  return files.map((f) => getCdnUrl(repo, `${dateFolder}/${f.path}`));
}

// Optional: pre-warm cache for known datasets on app init
export async function warmManifests(
  specs: Array<{ repo: string; dateFolder: string; token?: string }>
): Promise<void> {
  await Promise.allSettled(
    specs.map((s) => getDatasetManifest(s.repo, s.dateFolder, s.token))
  );
}
```

Example usage in a route/loader:

```ts
// src/routes/dataset/+page.ts (or +page.server.ts) — adjust to your framework
import { getDatasetFileUrls } from '$lib/data/hf-cdn';

const repo = 'myorg/vanguard-data';
const dateFolder = '2026-05-03';
const token = import.meta.env.VITE_HF_TOKEN; // optional; only needed for private repos or higher rate limits

const fileUrls = await getDatasetFileUrls(repo, dateFolder, token);
// Use fileUrls directly in <audio src={...}>, <video src={...}>, or fetch() for streaming
```

If you have an existing data module, replace its file enumeration with `getDatasetManifest`/`getCdnUrl` and remove recursive `/api/` listing and authenticated fetches for file bodies.

### 4. Verification (merged + actionable)

- **Cache presence**:  
  - Open DevTools → Application → Local Storage.  
  - Load dataset page; confirm key `hf-manifest:{repo}:{dateFolder}` with `files` array and recent `ts`.  
  - Refresh within 15 min; Network tab must show **no** `/api/repos/datasets/.../tree` call.

- **CDN URLs**:  
  - Confirm constructed URLs match `https://huggingface.co/datasets/{repo}/resolve/main/{dateFolder}/{path}`.  
  - Fetch one URL in Network tab; verify **no Authorization header** is sent and response is 200.

- **Fallback behavior**:  
  - Block HF API domain in DevTools (or throttle to 429) and reload.  
  - UI should still render using stale cache; no uncaught errors in Console.

- **Quota/429 reduction**:  
  - Monitor `HF API` calls in Network before/after. Expect >90% reduction in `/tree` and `/api` calls for repeat visits.  
  - Track `
