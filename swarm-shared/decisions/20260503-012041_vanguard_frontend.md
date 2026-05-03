# vanguard / frontend

## 1. Diagnosis

- Frontend still calls authenticated `list_repo_tree` on page loads, burning HF API quota and risking 429s.
- Downloads use authenticated `/api/` paths instead of public CDN URLs, wasting rate-limit budget.
- No persisted `(repo, dateFolder)` manifest; each client re-lists the same folder repeatedly.
- No fallback when HF API is rate-limited; UX stalls or errors instead of degrading gracefully.
- Missing lightweight client-side cache layer (memory + sessionStorage) for manifests and file URLs.

## 2. Proposed change

- Create `frontend/src/lib/api/file-manifest.ts` — manifest service with CDN URL builder, memory+sessionStorage cache, and optional backend fetch fallback.
- Refactor `frontend/src/lib/api/data.ts` — replace direct `list_repo_tree` calls with `getFileManifest(repo, dateFolder)` and use `cdnUrlFor(path)` for downloads.
- Add env var `VITE_HF_REPO` and optional `VITE_MANIFEST_API` (backend route) to control sources.

## 3. Implementation

```bash
# create files
touch /opt/axentx/vanguard/frontend/src/lib/api/file-manifest.ts
```

```typescript
// frontend/src/lib/api/file-manifest.ts
const HF_REPO = import.meta.env.VITE_HF_REPO || 'datasets/your-repo';
const MANIFEST_API = import.meta.env.VITE_MANIFEST_API; // e.g. /api/manifest

type FileEntry = { path: string; size?: number; type: 'file' | 'dir' };
type Manifest = { repo: string; dateFolder: string; files: FileEntry[]; ts: number };

const MEMO = new Map<string, Manifest>();
const CACHE_TTL = 1000 * 60 * 10; // 10m

function cacheKey(repo: string, dateFolder: string) {
  return `${repo}:${dateFolder}`;
}

function isStale(m: Manifest) {
  return Date.now() - m.ts > CACHE_TTL;
}

// Public CDN URL — NO Authorization header, bypasses /api/ rate limits
export function cdnUrlFor(repo: string, path: string): string {
  return `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;
}

// List files for a dateFolder (memory -> sessionStorage -> backend -> HF API)
export async function getFileManifest(
  repo: string,
  dateFolder: string,
  { forceRefresh = false } = {}
): Promise<Manifest> {
  const key = cacheKey(repo, dateFolder);

  // Memory cache (fastest)
  if (!forceRefresh) {
    const cached = MEMO.get(key);
    if (cached && !isStale(cached)) return cached;
  }

  // sessionStorage cache (persists across page reloads)
  if (!forceRefresh && typeof sessionStorage !== 'undefined') {
    try {
      const raw = sessionStorage.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw) as Manifest;
        if (!isStale(parsed)) {
          MEMO.set(key, parsed);
          return parsed;
        }
      }
    } catch {
      // ignore
    }
  }

  let files: FileEntry[] = [];

  // Prefer backend manifest route (avoids client-side HF API calls entirely)
  if (MANIFEST_API) {
    try {
      const res = await fetch(`${MANIFEST_API}/${encodeURIComponent(repo)}/${encodeURIComponent(dateFolder)}`);
      if (res.ok) {
        const payload = await res.json();
        if (Array.isArray(payload.files)) {
          files = payload.files;
        }
      }
    } catch {
      // fallback to HF CDN-only strategy below
    }
  }

  // If no backend or backend failed, do a one-time HF API list (from client) — use non-recursive folder list
  if (files.length === 0) {
    // Note: HF API rate limits apply here. Keep this as fallback only.
    // If you control deployments, prefer backend route + CDN-only fetching.
    const apiUrl = `https://huggingface.co/api/datasets/${encodeURIComponent(repo)}/tree/${encodeURIComponent(dateFolder)}`;
    const res = await fetch(apiUrl);
    if (!res.ok) {
      // Graceful degradation: return minimal manifest so UI can still attempt CDN downloads for known patterns
      console.warn('HF tree API failed, returning minimal manifest', res.status);
      files = [{ path: `${dateFolder}/`, type: 'dir' }];
    } else {
      const tree = await res.json();
      files = Array.isArray(tree)
        ? tree.map((t: any) => ({ path: t.path || '', type: t.type === 'tree' ? 'dir' : 'file', size: t.size }))
        : [];
    }
  }

  const manifest: Manifest = { repo, dateFolder, files, ts: Date.now() };
  MEMO.set(key, manifest);
  if (typeof sessionStorage !== 'undefined') {
    try {
      sessionStorage.setItem(key, JSON.stringify(manifest));
    } catch {
      // ignore storage quota errors
    }
  }
  return manifest;
}

// Utility: pick parquet files (or other patterns) from manifest
export function filterParquet(manifest: Manifest) {
  return manifest.files.filter((f) => f.type === 'file' && f.path.endsWith('.parquet'));
}
```

```typescript
// frontend/src/lib/api/data.ts  (refactor snippet — integrate)
import { getFileManifest, cdnUrlFor, filterParquet } from './file-manifest';

const HF_REPO = import.meta.env.VITE_HF_REPO || 'datasets/your-repo';

export async function listParquetFiles(dateFolder: string) {
  const manifest = await getFileManifest(HF_REPO, dateFolder);
  const parquetFiles = filterParquet(manifest);
  return parquetFiles.map((f) => ({
    path: f.path,
    url: cdnUrlFor(HF_REPO, f.path),
    size: f.size,
  }));
}

// Example usage in components:
// const files = await listParquetFiles('2026-04-29');
// files[0].url -> https://huggingface.co/datasets/.../resolve/main/2026-04-29/file.parquet
```

## 4. Verification

1. Build and run dev server:
   ```bash
   cd /opt/axentx/vanguard/frontend
   npm run dev
   ```
2. Open browser devtools → Network:
   - Navigate to a page that lists files for a `dateFolder`.
   - Confirm no authenticated `/api/` requests to `huggingface.co/api/...` occur (or only one fallback if backend route missing).
   - Confirm file downloads use `https://huggingface.co/datasets/.../resolve/main/...` (CDN) and those requests do NOT include an `Authorization` header.
3. Check caching:
   - Reload page; second load should not trigger `tree` API call (memory/sessionStorage hit).
   - In Network, verify only one request to `tree` (or none if backend manifest used).
4. Simulate HF API 429:
   - Temporarily block the tree API (e.g., via devtools) and confirm UI still renders (graceful degradation) and `cdnUrlFor` links remain valid.
5. Optional backend route test (if implemented):
   - Hit `/api/manifest/{repo}/{dateFolder}` and confirm JSON `{ repo, dateFolder, files: [...] }`.
   - Confirm frontend uses that route and skips HF tree API entirely.
