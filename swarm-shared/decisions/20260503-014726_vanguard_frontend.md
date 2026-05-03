# vanguard / frontend

## Final synthesized plan (correctness + concrete actionability)

**Core problem**: repeated authenticated `list_repo_tree` calls burn HF quota and cause 429s.  
**Correct resolution**: eliminate repeated auth calls by caching the file list and using CDN URLs for file access.  
**Chosen approach**: combine Candidate 1’s frontend cache + CDN fetch (fast, zero auth, works offline) with Candidate 2’s server-side manifest persistence (survives page reloads, shareable across users, reduces bootstrap cost).

---

## 1. Architecture (merged)

- **Frontend cache** (Candidate 1): in-memory + `localStorage` TTL cache for instant session reuse and graceful fallback on 429.
- **Server-side persisted manifest** (Candidate 2): JSON files under `/opt/axentx/vanguard/static/manifests/` keyed by `repo` and `dateFolder`, generated once and served without HF API.
- **Lightweight endpoint** (Candidate 2): `/api/manifest?repo=x&date=y` that:
  - returns cached manifest if present and fresh
  - optionally bootstraps once via HF API if missing (with locking to avoid stampede)
  - never proxies file content; only returns file paths
- **CDN-only file access** (Candidate 1): frontend uses `https://huggingface.co/datasets/{repo}/resolve/main/{filePath}` for previews/streaming (no auth, no quota).

---

## 2. Files to create/modify

### 2.1 `src/lib/store/hfCacheStore.ts` (Candidate 1 — keep)
```ts
type CacheEntry<T> = { data: T; expiresAt: number };
const STORAGE_PREFIX = 'hf-cache-v1:';

export function getCache<T>(key: string): T | null {
  try {
    const stored = localStorage.getItem(STORAGE_PREFIX + key);
    if (!stored) return null;
    const entry: CacheEntry<T> = JSON.parse(stored);
    if (Date.now() > entry.expiresAt) {
      localStorage.removeItem(STORAGE_PREFIX + key);
      return null;
    }
    return entry.data;
  } catch {
    return null;
  }
}

export function setCache<T>(key: string, data: T, ttlMs: number = 24 * 60 * 60 * 1000) {
  try {
    const entry: CacheEntry<T> = { data, expiresAt: Date.now() + ttlMs };
    localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(entry));
  } catch {
    // ignore quota limits
  }
}

export function invalidateCache(key: string) {
  try {
    localStorage.removeItem(STORAGE_PREFIX + key);
  } catch {}
}
```

### 2.2 `src/lib/data/hfClient.ts` (Candidate 1 — adapt)
```ts
import { PUBLIC_HF_API_BASE = 'https://huggingface.co/api' } from '$env/static/public';
import { getCache, setCache } from '$lib/store/hfCacheStore';

export interface RepoFile {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

// Prefer server manifest; fallback to HF API only when needed
export async function getDatasetFileListCached(
  repo: string,
  dateFolder: string,
  options?: { ttlMs?: number; forceRefresh?: boolean }
): Promise<string[]> {
  const cacheKey = `manifest:${repo}:${dateFolder}`;
  if (!options?.forceRefresh) {
    const cached = getCache<string[]>(cacheKey);
    if (cached) return cached;
  }

  // Try server manifest endpoint first (no HF auth)
  try {
    const res = await fetch(`/api/manifest?repo=${encodeURIComponent(repo)}&date=${encodeURIComponent(dateFolder)}`);
    if (res.ok) {
      const files: string[] = await res.json();
      setCache(cacheKey, files, options?.ttlMs ?? 24 * 60 * 60 * 1000);
      return files;
    }
  } catch {
    // proceed to HF API fallback
  }

  // Fallback: HF API (authenticated)
  const url = `${PUBLIC_HF_API_BASE}/datasets/${repo}/tree?path=${encodeURIComponent(dateFolder)}&recursive=false`;
  const res = await fetch(url, { headers: { Accept: 'application/json' } });

  if (!res.ok) {
    const stale = getCache<string[]>(cacheKey);
    if (stale) return stale;
    throw new Error(`HF API error: ${res.status} ${res.statusText}`);
  }

  const tree: Array<{ path: string; type: 'file' | 'directory' }> = await res.json();
  const files = tree.filter((n) => n.type === 'file').map((n) => `${dateFolder}/${n.path}`);

  setCache(cacheKey, files, options?.ttlMs ?? 24 * 60 * 60 * 1000);
  return files;
}

// CDN-only URL (no Authorization header required)
export function getCdnFileUrl(repo: string, filePath: string): string {
  return `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

// Optional: lightweight CDN fetch
export async function fetchCdnFile(repo: string, filePath: string, options?: RequestInit & { timeoutMs?: number }): Promise<Response> {
  const url = getCdnFileUrl(repo, filePath);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options?.timeoutMs ?? 30_000);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    return res;
  } finally {
    clearTimeout(timeout);
  }
}
```

### 2.3 Server-side manifest persistence (Candidate 2 — add)

Create: `/opt/axentx/vanguard/static/manifests/` (gitignored or cleaned periodically).  
Create: server endpoint `/src/routes/api/manifest/+server.ts` (SvelteKit):

```ts
// src/routes/api/manifest/+server.ts
import type { RequestHandler } from './$types';
import { getCache, setCache } from '$lib/store/hfCacheStore';
import { PUBLIC_HF_API_BASE = 'https://huggingface.co/api' } from '$env/static/public';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MANIFESTS_DIR = path.resolve(__dirname, '../../../../static/manifests');

function manifestPath(repo: string, date: string) {
  const safeRepo = repo.replace(/[^a-zA-Z0-9\-_/]/g, '_');
  const safeDate = date.replace(/[^0-9\-]/g, '_');
  return path.join(MANIFESTS_DIR, `${safeRepo}__${safeDate}.json`);
}

function readManifest(repo: string, date: string): string[] | null {
  try {
    const p = manifestPath(repo, date);
    if (!fs.existsSync(p)) return null;
    const raw = fs.readFileSync(p, 'utf8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function writeManifest(repo: string, date: string, files: string[]) {
  try {
    if (!fs.existsSync(MANIFESTS_DIR)) fs.mkdirSync(MANIFESTS_DIR, { recursive: true });
    const p = manifestPath(repo, date);
    fs.writeFileSync(p, JSON.stringify(files, null, 0), 'utf8');
  } catch {
    // ignore
  }
}

export const GET: RequestHandler = async ({ url }) => {
  const repo = url.searchParams.get('repo');
  const date = url.searchParams.get('date');
  if (!repo || !date) {
    return new Response('Missing repo or date', { status: 400 });
  }

  const cacheKey = `manifest:${repo}:${date}`;
  // Try in-memory/localStorage-style cache first (fast)
  const cached = getCache<string[]>(cacheKey);
  if (cached) return new Response(JSON.stringify(cached), { headers: { 'Content-Type': 'application
