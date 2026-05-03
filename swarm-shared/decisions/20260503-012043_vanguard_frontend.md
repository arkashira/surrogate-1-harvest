# vanguard / frontend

## 1. Diagnosis

- Frontend still performs authenticated HF API calls (`list_repo_tree`) on page load or training start, burning 1000/5min quota and causing 429s.
- Data fetches use authenticated `/api/` paths instead of public CDN URLs, wasting rate-limit headroom on downloads that could be anonymous.
- No persisted `(repo, dateFolder)` manifest exists, so every session repeats expensive listing calls.
- No client-side guard to reuse an already-running Lightning Studio instance, risking quota waste and idle-timeout training loss.
- Missing lightweight file-list cache layer (JSON) that can be embedded or served so training runs use CDN-only fetches with zero API calls during data load.

## 2. Proposed change

Create `frontend/src/lib/api/file-manifest.ts` (new) and refactor `frontend/src/lib/api/data.ts` to:
- Fetch or reuse a persisted manifest for `(repo, dateFolder)` (backend route or localStorage fallback).
- Convert dataset file URLs to public CDN form (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`).
- Add util to pick/start Lightning Studio with reuse-first logic.

Scope:
- New file: `frontend/src/lib/api/file-manifest.ts`
- Modify: `frontend/src/lib/api/data.ts`
- Optional: add lightweight server route `/api/manifest/:repo/:dateFolder` (if backend exists) — if not, fallback to localStorage + build-time JSON.

## 3. Implementation

### New: frontend/src/lib/api/file-manifest.ts

```ts
// frontend/src/lib/api/file-manifest.ts
import type { FileManifest } from '$lib/types';

const CDN_ROOT = 'https://huggingface.co/datasets';

export function toCdnUrl(repo: string, path: string): string {
  // Use public CDN URL (no auth) to bypass HF API rate limits
  return `${CDN_ROOT}/${repo}/resolve/main/${path}`;
}

export async function fetchManifest({
  repo,
  dateFolder,
  backendUrl = '/api/manifest',
}: {
  repo: string;
  dateFolder: string;
  backendUrl?: string;
}): Promise<FileManifest | null> {
  // 1) Try backend manifest endpoint (fast, server-cached)
  try {
    const res = await fetch(`${backendUrl}/${encodeURIComponent(repo)}/${encodeURIComponent(dateFolder)}`, {
      credentials: 'same-origin',
    });
    if (res.ok) {
      const json = await res.json();
      return normalizeManifest(json);
    }
  } catch {
    // fallback
  }

  // 2) Try localStorage cache (client-side persisted across reloads)
  const cached = readLocalManifest(repo, dateFolder);
  if (cached) return cached;

  // 3) If no manifest available, return null — caller should generate via one-time authenticated call
  return null;
}

export function saveManifestLocal({
  repo,
  dateFolder,
  manifest,
}: {
  repo: string;
  dateFolder: string;
  manifest: FileManifest;
}) {
  try {
    const key = `manifest::${repo}::${dateFolder}`;
    localStorage.setItem(key, JSON.stringify({ manifest, ts: Date.now() }));
  } catch {
    // ignore storage limits
  }
}

function readLocalManifest(repo: string, dateFolder: string): FileManifest | null {
  try {
    const key = `manifest::${repo}::${dateFolder}`;
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    // 24h TTL for localStorage fallback
    if (Date.now() - (parsed.ts ?? 0) > 86_400_000) return null;
    return normalizeManifest(parsed.manifest);
  } catch {
    return null;
  }
}

function normalizeManifest(json: any): FileManifest | null {
  if (!Array.isArray(json?.files)) return null;
  return {
    repo: String(json.repo || ''),
    dateFolder: String(json.dateFolder || ''),
    files: json.files.map((f: any) => ({
      path: String(f.path || ''),
      size: Number(f.size || 0),
      // ensure CDN-ready URL is available
      cdnUrl: f.cdnUrl || toCdnUrl(String(json.repo || ''), String(f.path || '')),
    })),
  };
}
```

### Modify: frontend/src/lib/api/data.ts

```ts
// frontend/src/lib/api/data.ts
import { fetchManifest, toCdnUrl } from './file-manifest';
import type { FileManifest } from '$lib/types';

// Lightweight client-side fallback when no backend manifest exists:
// perform ONE authenticated list (caller should guard this) and cache result.
export async function ensureManifest({
  repo,
  dateFolder,
  signal,
}: {
  repo: string;
  dateFolder: string;
  signal?: AbortSignal;
}): Promise<FileManifest | null> {
  const cached = await fetchManifest({ repo, dateFolder });
  if (cached) return cached;

  // If no backend and no cache, caller should avoid calling this repeatedly.
  // Provide an escape hatch: return null and let UI show "manifest missing — generate once".
  return null;
}

// Use CDN URLs for actual file content to avoid authenticated downloads
export function buildDatasetFileUrl(repo: string, filePath: string): string {
  return toCdnUrl(repo, filePath);
}

// Optional: helper to batch prefetch file metadata using CDN HEAD/GET (no auth)
export async function prefetchFileAvailability(
  files: Array<{ cdnUrl: string }>,
  { concurrency = 6, timeout = 8000 } = {}
): Promise<Array<{ cdnUrl: string; ok: boolean; status?: number }>> {
  const results: Array<{ cdnUrl: string; ok: boolean; status?: number }> = [];
  const queue = [...files];
  const workers = Array.from({ length: concurrency }).map(async () => {
    while (queue.length) {
      const item = queue.shift();
      if (!item) break;
      try {
        const controller = new AbortController();
        const id = setTimeout(() => controller.abort(), timeout);
        const res = await fetch(item.cdnUrl, { method: 'HEAD', signal: controller.signal });
        clearTimeout(id);
        results.push({ cdnUrl: item.cdnUrl, ok: res.ok, status: res.status });
      } catch {
        results.push({ cdnUrl: item.cdnUrl, ok: false });
      }
    }
  });
  await Promise.all(workers);
  return results;
}
```

### Optional: minimal server route (if backend exists)

If there’s a backend, add one lightweight endpoint:

```ts
// Example (Node/Express-like pseudo)
// GET /api/manifest/:repo/:dateFolder
// Returns pre-generated or on-demand manifest (cached)
```

Implementation plan for backend (if available):
- Add route that calls HF API `list_repo_tree` once per `(repo, dateFolder)` (with backoff/retry), project to `{ path, size }`, and store in KV/Redis/local cache with TTL 24h.
- Return JSON `{ repo, dateFolder, files: [...] }`.
- Frontend `fetchManifest` will consume it and avoid future authenticated calls.

### Lightning Studio reuse helper (frontend util)

```ts
// frontend/src/lib/lightning/studio.ts
import type { Studio } from 'lightning-ai'; // adjust import to actual SDK

export async function getOrCreateStudio(opts: {
  name: string;
  machine: string;
  onCreate?: (studio: Studio) => Promise<void> | void;
}): Promise<Studio | null> {
  try {
    // List existing studios in teamspace and reuse running one
    const studios = await Teamspace.studios?.() || [];
    const running = studios.find((s) => s.name === opts.name && s.status === 'Running');
    if (running) return running;

    // Otherwise create (or start stopped one)
    const studio = await Studio.create({
      name: opts.name,
      machine: opts.machine,
      create_ok: true,
    });
    if (opts.onCreate) await opts.onCreate(studio);
    return studio;
  } catch (err) {
    console.error('Studio reuse failed', err);
    return null;
