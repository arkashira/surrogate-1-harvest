# vanguard / frontend

## 1. Diagnosis

- Frontend still likely calls authenticated HF API (`list_repo_tree`, dataset metadata) on page load or training start, burning 1000/5min quota and causing 429s.
- No persisted `(repo, dateFolder)` file manifest; repeated listing requests are unnecessary when file list is static per folder.
- File fetches probably use authenticated `/api/` paths instead of public CDN URLs, wasting rate-limit budget on downloads that could be anonymous.
- No client-side caching layer for manifests; each navigation or refresh repeats listing calls.
- Missing fallback behavior when rate-limited (no exponential backoff, no CDN-only mode).

## 2. Proposed change

Create `frontend/src/lib/api/file-manifest.ts` (new) and refactor `frontend/src/lib/api/data.ts` to:

- Expose `getFileManifest(repo: string, dateFolder: string): Promise<FileManifest>` that:
  - Tries `localStorage`/`sessionStorage` cache keyed by `manifest:{repo}:{dateFolder}` first.
  - If miss, calls optional backend `/api/manifest/:repo/:dateFolder` (preferred) or, if unavailable, falls back to a single authenticated `list_repo_tree` call for that folder and caches result (TTL 24h).
- Expose `getCdnUrl(repo: string, dateFolder: string, filePath: string): string` that returns public CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{dateFolder}/{filePath}`) for downloads.
- Update data-loading code in `data.ts` to use CDN URLs and the manifest helper so training/data-preview flows make zero authenticated API calls during fetches.

## 3. Implementation

Create `frontend/src/lib/api/file-manifest.ts`:

```ts
// frontend/src/lib/api/file-manifest.ts
const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h

export interface FileEntry {
  path: string;
  size?: number;
  type?: 'file' | 'directory';
}

export interface FileManifest {
  repo: string;
  dateFolder: string;
  files: FileEntry[];
  generatedAt: number;
}

function cacheKey(repo: string, dateFolder: string): string {
  return `manifest:${repo}:${dateFolder}`;
}

function isFresh(manifest: FileManifest): boolean {
  return Date.now() - manifest.generatedAt < CACHE_TTL_MS;
}

export async function getFileManifest(
  repo: string,
  dateFolder: string,
  opts?: { preferBackend?: boolean; backendBase?: string }
): Promise<FileManifest> {
  const key = cacheKey(repo, dateFolder);
  const raw = localStorage.getItem(key);
  if (raw) {
    try {
      const cached: FileManifest = JSON.parse(raw);
      if (isFresh(cached)) return cached;
    } catch {
      localStorage.removeItem(key);
    }
  }

  let manifest: FileManifest | null = null;

  // 1) Try backend route (cheapest, avoids frontend HF API quota)
  if (opts?.preferBackend !== false) {
    try {
      const base = opts?.backendBase || '';
      const res = await fetch(`${base}/api/manifest/${encodeURIComponent(repo)}/${encodeURIComponent(dateFolder)}`, {
        credentials: 'include',
      });
      if (res.ok) {
        manifest = await res.json();
      }
    } catch {
      // fallback to direct listing
    }
  }

  // 2) Fallback: single authenticated list_repo_tree for folder (keep this lightweight)
  if (!manifest) {
    // NOTE: This call should be avoided in production by deploying the backend manifest route.
    // Kept as fallback for dev.
    manifest = await fetchFolderTreeAsManifest(repo, dateFolder);
  }

  manifest.generatedAt = Date.now();
  try {
    localStorage.setItem(key, JSON.stringify(manifest));
  } catch {
    // ignore storage quota errors
  }
  return manifest;
}

async function fetchFolderTreeAsManifest(repo: string, dateFolder: string): Promise<FileManifest> {
  // Replace with your actual HF client call. This is a placeholder that assumes a backend-like shape.
  // In practice, call your HF token-authenticated endpoint or SDK from here only if necessary.
  const prefix = `${dateFolder}/`;
  // Example using fetch to a backend proxy; if no proxy exists, this will need an authenticated HF API call.
  const res = await fetch(`/api/hf/tree?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(prefix)}`, {
    credentials: 'include',
  });
  if (!res.ok) {
    throw new Error('Failed to fetch folder tree');
  }
  const tree = await res.json(); // expect array of { path, type, size? }
  return {
    repo,
    dateFolder,
    files: Array.isArray(tree) ? tree : [],
    generatedAt: Date.now(),
  };
}

export function getCdnUrl(repo: string, dateFolder: string, filePath: string): string {
  const normalized = filePath.startsWith('/') ? filePath.slice(1) : filePath;
  const fullPath = `${dateFolder}/${normalized}`;
  return `https://huggingface.co/datasets/${encodeURIComponent(repo)}/resolve/main/${encodeURIComponent(fullPath)}`;
}

export function getManifestCachedOnly(repo: string, dateFolder: string): FileManifest | null {
  const key = cacheKey(repo, dateFolder);
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  try {
    const cached: FileManifest = JSON.parse(raw);
    return isFresh(cached) ? cached : null;
  } catch {
    return null;
  }
}
```

Update `frontend/src/lib/api/data.ts` (example changes):

```ts
// frontend/src/lib/api/data.ts
import { getFileManifest, getCdnUrl } from './file-manifest';

// Example loader that uses CDN URLs + manifest to avoid authenticated HF API during fetches.
export async function loadParquetPreview(repo: string, dateFolder: string, fileName: string) {
  // Use manifest to validate file exists (cached when possible)
  const manifest = await getFileManifest(repo, dateFolder, { preferBackend: true });
  const exists = manifest.files.some((f) => f.path === fileName || f.path === `${dateFolder}/${fileName}`);
  if (!exists) {
    throw new Error(`File not found in folder: ${fileName}`);
  }

  // Fetch via public CDN (no Authorization header) — bypasses HF API rate limits
  const url = getCdnUrl(repo, dateFolder, fileName);
  const res = await fetch(url);
  if (!res.ok) throw new Error('Failed to fetch file from CDN');
  const buffer = await res.arrayBuffer();
  // ... parse parquet or return raw bytes as needed
  return buffer;
}
```

If you want an optional backend route (recommended), add in backend (example):

```ts
// backend/src/routes/manifest.ts  (conceptual)
import { FastifyInstance } from 'fastify';
import { list_repo_tree } from '../hf-client';

export default async function manifestRoute(fastify: FastifyInstance) {
  fastify.get<{ Params: { repo: string; dateFolder: string } }>(
    '/api/manifest/:repo/:dateFolder',
    async (req, reply) => {
      const { repo, dateFolder } = req.params;
      // Cache backend-side too (e.g., Redis / memory) to avoid repeated HF API calls across users
      const cached = await fastify.cache.get(`manifest:${repo}:${dateFolder}`);
      if (cached) return JSON.parse(cached);

      const tree = await list_repo_tree(repo, dateFolder, { recursive: false });
      const files = Array.isArray(tree) ? tree : [];
      const manifest = { repo, dateFolder, files, generatedAt: Date.now() };
      await fastify.cache.set(`manifest:${repo}:${dateFolder}`, JSON.stringify(manifest), 'EX', 86400);
      return manifest;
    }
  );
}
```

## 4. Verification

- Open the app and trigger a data-preview or training-config flow that previously caused HF API calls.
- Check browser DevTools Network:
  - No authenticated `/api/` requests to HuggingFace for file listing or file downloads during fetches.
 
