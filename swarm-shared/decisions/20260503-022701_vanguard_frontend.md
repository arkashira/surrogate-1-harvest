# vanguard / frontend

## Final Synthesized Implementation

### 1. Diagnosis (merged)
- **No frontend manifest cache**: every preview/training launch triggers authenticated HF API calls, burning quota and risking 429s.
- **No CDN-bypass**: data loads route through authenticated `/api/` endpoints instead of public `resolve/main/` URLs.
- **No persisted file-list artifact**: no JSON manifest stored for `(repo, dateFolder) → [filePaths]`, so repeated runs re-enumerate folders.
- **No offline-first layer**: network flakiness or quota exhaustion breaks UX.
- **No deterministic repo picker for siblings**: ingestion can hit HF commit caps.
- **No visual indicator**: users can’t tell when cache is stale or CDN fallback is active.

### 2. Proposed change (merged)
Create a frontend manifest cache + CDN-bypass layer in `/opt/axentx/vanguard/src/lib/manifest/` with:
- `manifest.ts`: single source of truth for `(repo, dateFolder)` file-list; persists to localStorage and hydrates from SSR.
- `cdn.ts`: converts repo paths to `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with exponential backoff.
- `types.ts`: strict types for manifest and cache.
- `useManifest` hook for components to get cached file-list + CDN URLs.
- Small UI banner in preview/training views showing cache status and CDN mode.
- Optional deterministic repo picker for sibling repos.

Scope:
- Create `src/lib/manifest/` with `types.ts`, `cdn.ts`, `manifest.ts`, `useManifest.ts`.
- Update preview/training pages to use `useManifest` and display status.
- Add build-time type-check and one unit test for cache set/get.

### 3. Implementation

```bash
# create structure
mkdir -p /opt/axentx/vanguard/src/lib/manifest
```

#### `src/lib/manifest/types.ts`
```ts
export interface ManifestEntry {
  path: string;
  size: number;
  sha256?: string;
}

export interface Manifest {
  repo: string;
  dateFolder: string;
  generatedAt: number; // epoch ms
  files: ManifestEntry[];
  ttl: number; // ms (default 1h)
}

export interface ManifestCache {
  [key: string]: Manifest; // key = `${repo}::${dateFolder}`
}

export interface RepoShard {
  owner: string;
  name: string;
  shardIndex: number;
}
```

#### `src/lib/manifest/cdn.ts`
```ts
const CDN_ROOT = 'https://huggingface.co/datasets';

export function toCdnUrl(repo: string, path: string): string {
  const cleanPath = path.replace(/^\/+/, '');
  return `${CDN_ROOT}/${repo}/resolve/main/${cleanPath}`;
}

export async function fetchViaCdn(url: string, opts: RequestInit = {}): Promise<Response> {
  const maxRetries = 3;
  let delay = 500;
  for (let i = 0; i <= maxRetries; i++) {
    try {
      const res = await fetch(url, { ...opts, cache: 'no-store' });
      if (res.ok) return res;
      if (res.status >= 500 || res.status === 429) throw new Error(`cdn_retry ${res.status}`);
      return res; // client errors are not retried
    } catch (err) {
      if (i === maxRetries) throw err;
      await new Promise((r) => setTimeout(r, delay));
      delay *= 2;
    }
  }
  throw new Error('unreachable');
}
```

#### `src/lib/manifest/manifest.ts`
```ts
import type { Manifest, ManifestCache } from './types';
import { fetchViaCdn, toCdnUrl } from './cdn';

const STORAGE_KEY = 'vanguard_manifest_cache_v1';
const DEFAULT_TTL = 60 * 60 * 1000; // 1h

function storage() {
  if (typeof window === 'undefined') {
    return { get: () => ({}), set: () => void 0 };
  }
  return {
    get: () => {
      try {
        return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') as ManifestCache;
      } catch {
        return {} as ManifestCache;
      }
    },
    set: (v: ManifestCache) => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
      } catch {
        // ignore quota errors
      }
    },
  };
}

function cacheKey(repo: string, dateFolder: string): string {
  return `${repo}::${dateFolder}`;
}

export function getCachedManifest(repo: string, dateFolder: string): (Manifest & { _stale?: boolean }) | null {
  const key = cacheKey(repo, dateFolder);
  const store = storage().get();
  const m = store[key];
  if (!m) return null;
  const stale = Date.now() - m.generatedAt > (m.ttl || DEFAULT_TTL);
  return stale ? { ...m, _stale: true } : m;
}

export function setCachedManifest(manifest: Manifest): void {
  const key = cacheKey(manifest.repo, manifest.dateFolder);
  const store = storage().get();
  store[key] = manifest;
  storage().set(store);
}

export async function fetchManifestFromApi(repo: string, dateFolder: string): Promise<Manifest> {
  // Lightweight API call to list one folder.
  // Prefer a backend endpoint if available to avoid frontend token exposure.
  const res = await fetch(`/api/hf/list?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(dateFolder)}`);
  if (!res.ok) throw new Error('Failed to fetch manifest');
  const tree = await res.json(); // expect array of { path, size, sha256? }
  const manifest: Manifest = {
    repo,
    dateFolder,
    generatedAt: Date.now(),
    ttl: DEFAULT_TTL,
    files: Array.isArray(tree) ? tree : [],
  };
  setCachedManifest(manifest);
  return manifest;
}

export async function getManifest(
  repo: string,
  dateFolder: string,
  opts?: { refresh?: boolean }
): Promise<Manifest> {
  const cached = opts?.refresh ? null : getCachedManifest(repo, dateFolder);
  if (cached && !cached._stale) return cached;
  try {
    return await fetchManifestFromApi(repo, dateFolder);
  } catch (err) {
    // fallback to stale cache if available
    if (cached) return cached;
    throw err;
  }
}

export function getFileCdnUrls(manifest: Manifest): Array<{ entry: ManifestEntry; url: string }> {
  return manifest.files.map((entry) => ({
    entry,
    url: toCdnUrl(manifest.repo, entry.path),
  }));
}

// Optional: deterministic repo picker for sibling repos (sharding)
export function pickShardRepo(baseOwner: string, baseName: string, shardIndex: number, totalShards: number): string {
  // Simple deterministic shard: baseName-shardN
  const shardName = `${baseName}-shard${shardIndex}`;
  return `${baseOwner}/${shardName}`;
}
```

#### `src/lib/manifest/useManifest.ts`
```ts
import { getManifest, getFileCdnUrls } from './manifest';
import type { Manifest } from './types';
import { useEffect, useState } from 'react';

export function useManifest(repo: string, dateFolder: string, opts?: { refresh?: boolean }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    getManifest(repo, dateFolder, opts)
      .then((m) => setManifest(m))
      .catch((e) => setError(e))
      .finally(() => setLoading(false));
  }, [repo, dateFolder, opts?.refresh]);

  const cdnUrls = manifest ? getFileCdnUrls(manifest) : [];
  const stale = manifest ? !!(manifest as Manifest
