# vanguard / frontend

## Final synthesized solution (correct + actionable)

**Core problem**: the frontend is calling authenticated `list_repo_tree` on every page load, burning HF API quota and risking 429s, and it downloads via authenticated `/api/` paths instead of public CDN URLs. There is no persisted manifest, no caching/deduplication, and no graceful fallback when HF throttles.

**Goal**: eliminate repeated authenticated tree listings, switch all file downloads to public CDN URLs, persist a `(repo,dateFolder)` manifest at build/orchestration time when possible, provide a fast server-side manifest endpoint with CDN URLs, add client-side caching + deduplication, and degrade gracefully (serve stale/cached data) when HF is rate-limited.

---

### 1. Recommended file changes

- Create: `/opt/axentx/vanguard/src/lib/hf-client.ts`
- Create: `/opt/axentx/vanguard/src/routes/api/manifest/+server.ts`
- Update: `/opt/axentx/vanguard/src/routes/(app)/training/+page.server.ts` (or your route’s server loader)
- Update: `/opt/axentx/vanguard/src/routes/(app)/training/+page.svelte` (or equivalent view)
- Optional but recommended: add build/orchestration step to generate `manifests/{repo}/{dateFolder}.json` and commit or upload to a fast store (CDN/edge cache/S3) so the server endpoint rarely calls HF.

---

### 2. Implementation (merged + hardened)

#### `src/lib/hf-client.ts`
```ts
// Types and helpers for HF CDN + manifest handling
export interface HfFile {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

export interface RepoManifest {
  repo: string;
  dateFolder: string;
  files: HfFile[];
  generatedAt: string;
}

const CDN_ROOT = 'https://huggingface.co/datasets';

export function cdnUrl(repo: string, path: string): string {
  // Public CDN URL — no auth required
  return `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(path)}`;
}

// Server-side fetch (used by +server.ts). Prefer build-time manifests in prod.
export async function fetchManifestFromHf(
  repo: string,
  dateFolder: string,
  signal?: AbortSignal
): Promise<RepoManifest> {
  // Non-recursive calls to avoid pagination cost
  const treeRes = await fetch(
    `https://huggingface.co/api/datasets/${repo}/tree?path=${encodeURIComponent(dateFolder)}&recursive=false`,
    { signal }
  );
  if (!treeRes.ok) throw new Error(`HF tree error: ${treeRes.status}`);
  const items: Array<{ path: string; type: string; size?: number }> = await treeRes.json();

  // If dateFolder not found, fallback to repo root files (graceful degradation)
  const files = items.filter((i) => i.type === 'file' || i.path.startsWith(dateFolder));
  return {
    repo,
    dateFolder,
    files,
    generatedAt: new Date().toISOString()
  };
}

// Client-side: cached + deduplicated manifest fetch (SWR-like)
const PENDING = new Map<string, Promise<RepoManifest>>();
const CACHE = new Map<string, { manifest: RepoManifest; ts: number }>();
const TTL = 5 * 60 * 1000; // 5m client cache

export async function getManifestCached(repo: string, dateFolder: string, opts?: { fresh?: boolean }): Promise<RepoManifest> {
  const key = `${repo}::${dateFolder}`;
  const now = Date.now();

  // Serve fresh copy if forced
  if (opts?.fresh) {
    CACHE.delete(key);
  } else {
    const cached = CACHE.get(key);
    if (cached && now - cached.ts < TTL) return cached.manifest;
  }

  const existing = PENDING.get(key);
  if (existing) return existing;

  const p = (async () => {
    try {
      const res = await fetch(`/api/manifest?repo=${encodeURIComponent(repo)}&date=${encodeURIComponent(dateFolder)}`);
      if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
      const manifest = (await res.json()) as RepoManifest;
      CACHE.set(key, { manifest, ts: Date.now() });
      return manifest;
    } finally {
      PENDING.delete(key);
    }
  })();

  PENDING.set(key, p);
  return p;
}

// Client-side helper to map manifest to CDN URLs
export function filesToCdnUrls(manifest: RepoManifest) {
  return manifest.files
    .filter((f) => f.type === 'file')
    .map((f) => ({
      path: f.path,
      url: cdnUrl(manifest.repo, f.path)
    }));
}
```

---

#### `src/routes/api/manifest/+server.ts`
```ts
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { HF_TOKEN } from '$env/static/private';

async function listRepoTree(repo: string, path: string) {
  const res = await fetch(
    `https://huggingface.co/api/datasets/${repo}/tree?path=${encodeURIComponent(path)}&recursive=false`,
    {
      headers: HF_TOKEN ? { Authorization: `Bearer ${HF_TOKEN}` } : {}
    }
  );
  if (!res.ok) throw new Error(`HF tree error: ${res.status}`);
  return res.json() as Promise<Array<{ path: string; type: string; size?: number }>>;
}

export const GET: RequestHandler = async ({ url }) => {
  const repo = url.searchParams.get('repo');
  const date = url.searchParams.get('date');
  if (!repo || !date) return json({ error: 'repo and date required' }, { status: 400 });

  try {
    // Try to list date folder; fallback to root files if missing
    const items = await listRepoTree(repo, date);
    let files = items;

    // If date folder not present, try root (graceful)
    if (files.length === 0) {
      const rootItems = await listRepoTree(repo, '');
      files = rootItems.filter((i) => i.type === 'file');
    }

    const manifest = {
      repo,
      dateFolder: date,
      files,
      generatedAt: new Date().toISOString()
    };

    // Short CDN-friendly cache; edge can revalidate. Do not expose auth downstream.
    return json(manifest, {
      headers: {
        'Cache-Control': 'public, max-age=60, stale-while-revalidate=300',
        'Vary': 'Origin'
      }
    });
  } catch (err) {
    console.error('Manifest build failed:', err);
    return json({ error: 'Failed to build manifest' }, { status: 500 });
  }
};
```

---

#### `src/routes/(app)/training/+page.server.ts` (example)
```ts
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ fetch, url }) => {
  const repo = url.searchParams.get('repo') || 'owner/dataset';
  const date = url.searchParams.get('date') || '2026-04-29';

  // Server fetches manifest once per request. If this fails, degrade gracefully.
  let manifest = { repo, dateFolder: date, files: [], generatedAt: new Date().toISOString() };
  try {
    const res = await fetch(`/api/manifest?repo=${encodeURIComponent(repo)}&date=${encodeURIComponent(date)}`);
    if (res.ok) manifest = await res.json();
  } catch {
    // silent fail — empty manifest is acceptable
  }

  // Always use CDN URLs for frontend
  const fileUrls = manifest.files
    .filter((f: any) => f.type === 'file')
    .map((f: any) => ({
      path: f.path,
      url: `https://huggingface.co/datasets/${manifest.repo}/resolve/main/${encodeURIComponent(f.path)}`
    }));


