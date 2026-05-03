# vanguard / frontend

### Final Synthesis (Best Parts + Correctness + Actionability)

**Diagnosis (resolved contradictions)**
- Repeated HF API enumeration on page load via authenticated `/api/` paths burns quota and causes 429s.  
- No persisted `(repo, dateFolder)` manifest on the client → refetch on every load/remount.  
- Data fetches use authenticated paths instead of public CDN URLs → unnecessary rate-limit pressure.  
- No client-side cache (memory + localStorage) and no graceful fallback when throttled → poor UX and fragility.  
- Missing retry/backoff and observability makes failures opaque.

**Proposed change (single, prioritized scope)**
- Add a lightweight `hf-client` module that:
  1. Fetches and caches a `(repo, dateFolder)` manifest (5-minute TTL) in localStorage.
  2. Builds and uses **public CDN URLs only** for data fetches.
  3. Implements memory + localStorage cache for fetched data.
  4. Adds exponential backoff retry for 429/5xx with jitter and a graceful CDN-only fallback when throttled.
  5. Exposes simple logging/metrics for cache hits, misses, and retries.
- Replace existing HF data fetch logic to route through this module.
- Keep changes minimal and framework-agnostic (works in Svelte/React/Vue) so it can be dropped into `/opt/axentx/vanguard/src/lib/hf-client.ts` and used in loaders/routes.

**Implementation (concrete, production-ready)**

```bash
# Ensure directory exists
mkdir -p /opt/axentx/vanguard/src/lib
```

```typescript
// /opt/axentx/vanguard/src/lib/hf-client.ts
const CDN_ROOT = 'https://huggingface.co/datasets';
const MANIFEST_TTL_MS = 5 * 60 * 1000; // 5 minutes
const MAX_RETRIES = 3;
const BASE_DELAY_MS = 1_000;

type ManifestEntry = { files: string[]; fetchedAt: number };

function memoryCache<T>() {
  const store = new Map<string, { value: T; ts: number }>();
  return {
    get(key: string) {
      const entry = store.get(key);
      if (!entry) return null;
      // 5-minute TTL for memory entries
      if (Date.now() - entry.ts > MANIFEST_TTL_MS) {
        store.delete(key);
        return null;
      }
      return entry.value;
    },
    set(key: string, value: T) {
      store.set(key, { value, ts: Date.now() });
    },
    has(key: string) {
      return this.get(key) !== null;
    },
  };
}

const memCache = memoryCache<any>();

function localStorageCache<T>() {
  return {
    get(key: string): T | null {
      try {
        const raw = localStorage.getItem(key);
        if (!raw) return null;
        const { value, ts } = JSON.parse(raw);
        if (Date.now() - ts > MANIFEST_TTL_MS) {
          localStorage.removeItem(key);
          return null;
        }
        return value as T;
      } catch {
        return null;
      }
    },
    set(key: string, value: T) {
      try {
        localStorage.setItem(
          key,
          JSON.stringify({ value, ts: Date.now() })
        );
      } catch (e) {
        // ignore quota errors
        console.warn('[hf-client] localStorage set failed', e);
      }
    },
  };
}

const lsCache = localStorageCache<any>();

function manifestKey(repo: string, dateFolder: string) {
  return `hf-manifest:${repo}:${dateFolder}`;
}

function dataKey(repo: string, dateFolder: string) {
  return `hf-data:${repo}:${dateFolder}`;
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function jitter(ms: number) {
  return ms * 0.5 + Math.random() * ms * 0.5;
}

async function fetchWithRetry(
  url: string,
  options: RequestInit = {},
  retries = MAX_RETRIES
): Promise<Response> {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, {
        ...options,
        // Avoid sending credentials to CDN; keep requests cacheable
        credentials: 'omit',
      });

      // 429 or 5xx -> retry with backoff
      if (res.status === 429 || (res.status >= 500 && res.status < 600)) {
        if (attempt === retries) return res;
        const wait = jitter(BASE_DELAY_MS * 2 ** attempt);
        console.warn(
          `[hf-client] ${res.status} on ${url}, retry ${attempt + 1}/${retries} in ${Math.round(wait)}ms`
        );
        await delay(wait);
        continue;
      }

      return res;
    } catch (err) {
      if (attempt === retries) throw err;
      const wait = jitter(BASE_DELAY_MS * 2 ** attempt);
      await delay(wait);
    }
  }

  throw new Error('Unexpected retry exhaustion');
}

/**
 * Fetch and cache manifest for (repo, dateFolder).
 * Returns list of files (or minimal metadata) from manifest.
 */
export async function getManifest(
  repo: string,
  dateFolder: string
): Promise<string[]> {
  const mKey = manifestKey(repo, dateFolder);

  // Memory -> localStorage -> network
  const fromMem = memCache.get<string[]>(mKey);
  if (fromMem) {
    console.debug('[hf-client] manifest memory hit', repo, dateFolder);
    return fromMem;
  }

  const fromLs = lsCache.get<string[]>(mKey);
  if (fromLs) {
    console.debug('[hf-client] manifest localStorage hit', repo, dateFolder);
    memCache.set(mKey, fromLs);
    return fromLs;
  }

  // Network: use CDN resolve endpoint (public) to validate folder exists.
  // If this 429s, we still want to fail fast and fallback gracefully elsewhere.
  const manifestUrl = `${CDN_ROOT}/${repo}/resolve/main/${dateFolder}`;
  const res = await fetchWithRetry(manifestUrl, { method: 'HEAD' });

  // If HEAD not allowed, fallback to a minimal synthetic manifest.
  // Many HF datasets allow raw file access; we'll build manifest from known patterns
  // or allow caller to provide file list. For robustness, return empty list so
  // callers can still try direct file paths.
  let files: string[] = [];
  if (res.ok) {
    // If HEAD succeeds, treat folder as valid; caller should know filenames.
    // We store a placeholder so we don't re-HEAD repeatedly.
    files = ['.valid'];
  }

  const entry: ManifestEntry = { files, fetchedAt: Date.now() };
  memCache.set(mKey, files);
  lsCache.set(mKey, files);
  console.debug('[hf-client] manifest fetched', repo, dateFolder, files);
  return files;
}

/**
 * Fetch a dataset file via public CDN URL with cache + retry.
 * Prefer this over any /api/... calls.
 */
export async function fetchDatasetFile(
  repo: string,
  dateFolder: string,
  filename: string,
  asJson = true
): Promise<any> {
  const dKey = dataKey(repo, dateFolder + ':' + filename);

  const fromMem = memCache.get(dKey);
  if (fromMem) {
    console.debug('[hf-client] data memory hit', repo, dateFolder, filename);
    return fromMem;
  }

  const fromLs = lsCache.get(dKey);
  if (fromLs) {
    console.debug('[hf-client] data localStorage hit', repo, dateFolder, filename);
    memCache.set(dKey, fromLs);
    return fromLs;
  }

  const url = `${CDN_ROOT}/${repo}/resolve/main/${dateFolder}/${filename}`;
  const res = await fetchWithRetry(url, { credentials: 'omit' });

  if (!res.ok) {
    // Graceful degradation: throw structured error so UI can fallback
    const err: any = new
