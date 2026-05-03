# vanguard / frontend

## Final Synthesized Implementation (Correct + Actionable)

**Chosen approach**: Client-side manifest caching + CDN-only fetches, with request deduplication, exponential backoff, and graceful fallback to stale cache when rate-limited.  
**Scope**: Frontend only (`/opt/axentx/vanguard/src`).  
**Primary files to add/modify**:
- `src/lib/hf-client.ts` (new)
- `src/hooks/useHFData.ts` (new)
- Update or create dataset/training loader components to use the hook.

---

### 1. Create `src/lib/hf-client.ts`

```ts
// src/lib/hf-client.ts
const API_BASE = 'https://huggingface.co/api';
const CDN_BASE = 'https://huggingface.co/datasets';

const CACHE_PREFIX = 'hf:manifest:v2';
const CACHE_TTL_MS = 1000 * 60 * 30; // 30 minutes

interface FileEntry {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

type CacheMeta = {
  entries: FileEntry[];
  ts: number;
};

function cacheKey(repo: string, path: string) {
  return `${CACHE_PREFIX}:${repo}:${path}`;
}

function getCached(repo: string, path: string): CacheMeta | null {
  try {
    const raw = localStorage.getItem(cacheKey(repo, path));
    if (!raw) return null;
    const meta: CacheMeta = JSON.parse(raw);
    if (Date.now() - meta.ts > CACHE_TTL_MS) return null;
    return meta;
  } catch {
    return null;
  }
}

function setCached(repo: string, path: string, entries: FileEntry[]) {
  try {
    const meta: CacheMeta = { entries, ts: Date.now() };
    localStorage.setItem(cacheKey(repo, path), JSON.stringify(meta));
  } catch {
    // ignore quota errors
  }
}

// In-flight deduplication map
const inflight = new Map<string, Promise<FileEntry[]>>();

function wait(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

export async function listRepoTreeOnce(
  repo: string,
  path: string = '',
  options: { maxRetries?: number; initialBackoffMs?: number; retryAfter429Ms?: number } = {}
): Promise<FileEntry[]> {
  const key = `${repo}:${path}`;
  if (inflight.has(key)) {
    return inflight.get(key)!;
  }

  const maxRetries = options.maxRetries ?? 3;
  const initialBackoffMs = options.initialBackoffMs ?? 1000;
  const retryAfter429Ms = options.retryAfter429Ms ?? 300_000; // 5 minutes

  const task = (async () => {
    // 1) Try fresh cache first (fast)
    const cached = getCached(repo, path);
    if (cached) return cached.entries;

    let lastError: Error | null = null;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        const res = await fetch(`${API_BASE}/repos/datasets/${repo}/tree`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, recursive: false }),
        });

        if (res.status === 429) {
          // If rate-limited, try stale cache before sleeping
          const stale = getCached(repo, path); // will be null if expired
          // Also try localStorage without TTL check as last resort
          if (!stale) {
            try {
              const raw = localStorage.getItem(cacheKey(repo, path));
              if (raw) {
                const parsed = JSON.parse(raw) as CacheMeta;
                if (parsed?.entries?.length) {
                  return parsed.entries;
                }
              }
            } catch {
              // ignore
            }
          }
          if (stale) return stale.entries;

          await wait(retryAfter429Ms);
          continue;
        }

        if (!res.ok) {
          throw new Error(`HF API error: ${res.status} ${res.statusText}`);
        }

        const tree = await res.json();
        const entries: FileEntry[] = Array.isArray(tree) ? tree : tree?.tree || [];
        setCached(repo, path, entries);
        return entries;
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
        // exponential backoff between retries
        await wait(initialBackoffMs * Math.pow(2, attempt));
      }
    }

    // Final fallback: try stale cache
    try {
      const raw = localStorage.getItem(cacheKey(repo, path));
      if (raw) {
        const parsed = JSON.parse(raw) as CacheMeta;
        if (parsed?.entries?.length) return parsed.entries;
      }
    } catch {
      // ignore
    }

    throw lastError || new Error('Failed to list repo tree');
  })();

  inflight.set(key, task);
  try {
    return await task;
  } finally {
    inflight.delete(key);
  }
}

export function toCdnUrl(repo: string, filePath: string): string {
  return `${CDN_BASE}/${repo}/resolve/main/${encodeURI(filePath)}`;
}

export async function fetchFileTextCdn(repo: string, filePath: string): Promise<string> {
  const url = toCdnUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch CDN file: ${res.status} ${res.statusText}`);
  return await res.text();
}

export async function fetchFileJsonCdn<T = any>(repo: string, filePath: string): Promise<T> {
  const url = toCdnUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch CDN JSON: ${res.status} ${res.statusText}`);
  return await res.json();
}
```

---

### 2. Create `src/hooks/useHFData.ts`

```ts
// src/hooks/useHFData.ts
import { useEffect, useState, useCallback } from 'react';
import { listRepoTreeOnce, toCdnUrl, fetchFileTextCdn, FileEntry } from '../lib/hf-client';

export function useHFData(repo: string, folderPath: string = '') {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const entries = await listRepoTreeOnce(repo, folderPath);
      setFiles(entries.filter((e) => e.type === 'file'));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [repo, folderPath]);

  useEffect(() => {
    load();
  }, [load]);

  const getFileUrl = useCallback((filePath: string) => toCdnUrl(repo, filePath), [repo]);

  const getFileText = useCallback(
    async (filePath: string) => fetchFileTextCdn(repo, filePath),
    [repo]
  );

  const getFileJson = useCallback(
    async <T = any>(filePath: string) => {
      const url = toCdnUrl(repo, filePath);
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Failed to fetch CDN JSON: ${res.status} ${res.statusText}`);
      return (await res.json()) as T;
    },
    [repo]
  );

  const refresh = load;

  return { files, loading, error, getFileUrl, getFileText, getFileJson, refresh };
}
```

---

### 3. Update / Create Loader Component(s)

If a dataset or training loader exists (e.g., `DatasetLoader.tsx`), replace authenticated fetches with the hook. Example:
