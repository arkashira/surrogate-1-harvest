# vanguard / frontend

## Final Synthesis — One Correct, Actionable Plan

**Core diagnosis (merged, de-duplicated):**
- Frontend re-lists HF repo files on every training launch via authenticated API calls → burns quota and risks 429.
- No persisted, per-date-folder HF file manifest → training cannot enforce CDN-only fetches; loaders may trigger runtime API calls.
- No client-side caching for repeated identical list requests.
- No deterministic routing for HF write commits → burst ingestion risks hitting commit cap.
- Missing UX affordance to reuse an existing Lightning Studio session instead of recreating.

**Chosen scope (concrete files):**
- `/opt/axentx/vanguard/src/frontend/lib/hf-client.ts` (new/modify) — manifest + CDN logic + caching + sibling repo selection.
- `/opt/axentx/vanguard/src/frontend/lib/hf-manifest.ts` (new) — manifest persistence/lookup and CDN streaming.
- `/opt/axentx/vanguard/src/frontend/components/TrainingLaunchForm.svelte` (or `.tsx` equivalent) — integrate manifest generation, reuse, and Studio session reuse.

---

## 1. Implementation — `hf-client.ts`

```ts
// /opt/axentx/vanguard/src/frontend/lib/hf-client.ts
import { get } from 'svelte/store';
import { trainingConfig } from '$lib/stores/training';

const HF_API_BASE = 'https://huggingface.co/api';
const HF_CDN_BASE = 'https://huggingface.co/datasets';
const HF_TOKEN = import.meta.env.VITE_HF_TOKEN || '';

type RepoFile = { path: string; type?: 'file' | 'directory' };
type ManifestMeta = { repo: string; dateFolder: string; files: Array<{ path: string }>; generatedAt: string };

const fileListCache = new Map<string, { files: RepoFile[]; ts: number; etag?: string }>();
const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

// Lightweight deterministic hash for sibling selection
function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h) + s.charCodeAt(i);
    h |= 0;
  }
  return h >>> 0;
}

// List repo tree once per (repo,path) with client-side cache and conditional request support
export async function listRepoTreeOnce(repo: string, path: string, { force = false } = {}): Promise<RepoFile[]> {
  const cacheKey = `${repo}::${path}`;
  const cached = fileListCache.get(cacheKey);

  if (!force && cached && Date.now() - cached.ts < CACHE_TTL_MS) {
    return cached.files;
  }

  const url = `${HF_API_BASE}/repos/datasets/${repo}/tree?path=${encodeURIComponent(path)}&recursive=false`;
  const headers: Record<string, string> = {};
  if (HF_TOKEN) headers.Authorization = `Bearer ${HF_TOKEN}`;
  if (cached?.etag) headers['If-None-Match'] = cached.etag;

  const res = await fetch(url, { headers });

  if (res.status === 304 && cached) {
    // Not modified — refresh timestamp and reuse
    fileListCache.set(cacheKey, { ...cached, ts: Date.now() });
    return cached.files;
  }

  if (!res.ok) {
    if (res.status === 429 && cached) return cached.files; // graceful fallback
    throw new Error(`HF tree list failed: ${res.status}`);
  }

  const etag = res.headers.get('ETag') || undefined;
  const tree = await res.json(); // array of { path, type }
  const files = Array.isArray(tree) ? tree.filter((n: any) => n.type === 'file') : [];
  fileListCache.set(cacheKey, { files, ts: Date.now(), etag });
  return files;
}

// Build CDN URL (no auth)
export function buildCdnUrl(repo: string, filePath: string): string {
  return `${HF_CDN_BASE}/${repo}/resolve/main/${encodeURI(filePath)}`;
}

// Fetch file via CDN (no Authorization header)
export async function fetchViaCdn(repo: string, filePath: string, signal?: AbortSignal): Promise<Response> {
  return fetch(buildCdnUrl(repo, filePath), { signal });
}

// Deterministic sibling repo selector for writes to spread commits
export function pickSiblingRepo(baseRepo: string, nSiblings: number = 5): string {
  const idx = hashString(baseRepo) % nSiblings;
  return idx === 0 ? baseRepo : `${baseRepo}-sibling-${idx}`;
}

// Persist manifest for browser download (for user to provide to backend/launcher)
export function persistManifestBrowser(meta: ManifestMeta): void {
  const blob = new Blob([JSON.stringify(meta, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `manifests/${meta.dateFolder}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Optional: if running in Node/SSR context, allow writing manifest to server storage
export async function persistManifestServer(
  meta: ManifestMeta,
  fsWrite: (path: string, content: string) => Promise<void>,
  outDir: string = 'manifests'
): Promise<string> {
  const outPath = `${outDir}/${meta.dateFolder}.json`;
  await fsWrite(outPath, JSON.stringify(meta, null, 2));
  return outPath;
}
```

---

## 2. Implementation — `hf-manifest.ts`

```ts
// /opt/axentx/vanguard/src/frontend/lib/hf-manifest.ts
import { listRepoTreeOnce, persistManifestBrowser, persistManifestServer, type ManifestMeta } from './hf-client';

// Ensure a manifest exists: generate if missing/forced and persist (browser download or server write)
export async function ensureManifest(
  repo: string,
  dateFolder: string,
  options?: {
    force?: boolean;
    mode?: 'browser' | 'server';
    fsWrite?: (path: string, content: string) => Promise<void>;
    outDir?: string;
  }
): Promise<ManifestMeta> {
  const { force = false, mode = 'browser', fsWrite, outDir = 'manifests' } = options || {};
  const files = await listRepoTreeOnce(repo, dateFolder, { force });
  const meta: ManifestMeta = {
    repo,
    dateFolder,
    files: files.map((f) => ({ path: f.path })),
    generatedAt: new Date().toISOString(),
  };

  if (mode === 'server' && fsWrite) {
    await persistManifestServer(meta, fsWrite, outDir);
  } else {
    persistManifestBrowser(meta);
  }
  return meta;
}

// Stream files from manifest using CDN-only URLs
export async function streamFilesFromManifest(
  manifest: ManifestMeta,
  onFile: (path: string, content: ArrayBuffer) => void,
  signal?: AbortSignal
): Promise<void> {
  for (const f of manifest.files) {
    if (signal?.aborted) break;
    const res = await fetchViaCdn(manifest.repo, f.path, signal);
    if (!res.ok) continue;
    const buf = await res.arrayBuffer();
    onFile(f.path, buf);
  }
}
```

---

## 3. Implementation — `TrainingLaunchForm.svelte` (integrated)

```svelte
<!-- /opt/axentx/vanguard/src/frontend/components/TrainingLaunchForm.svelte -->
<script lang="ts">
  import { ensureManifest, pickSiblingRepo } from '$lib/hf-manifest';
  import { trainingConfig } from '$lib/stores/training';
  import { onMount } from 'svelte';

  let repo = 'axentx/surrogate-1';
  let dateFolder = '2026-05-02';
  let generating = false;
  let manifest: any = null;
  let studioSessionId: string |
