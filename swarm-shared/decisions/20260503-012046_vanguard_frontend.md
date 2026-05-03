# vanguard / frontend

## 1. Diagnosis

- Frontend still calls authenticated `list_repo_tree` on page loads, burning HF API quota and risking 429s.
- Data fetches use authenticated `/api/` paths instead of public CDN URLs, wasting rate-limit budget and adding latency.
- No persisted `(repo, dateFolder)` manifest means every session repeats expensive tree walks.
- No fallback when HF API is rate-limited; training/CDN assets become unavailable in the UI.
- Missing lightweight caching layer (memory + localStorage) for manifests and CDN URL templates.

## 2. Proposed change

- Create `frontend/src/lib/api/file-manifest.ts` — manifest generation + caching.
- Refactor `frontend/src/lib/api/data.ts` — use CDN URLs and the manifest cache; add graceful fallback.
- Add optional lightweight backend route `/api/manifest/:repo/:dateFolder` (if backend exists) to serve pre-generated or on-demand manifests (kept minimal; frontend-first).
- Scope: ~120–180 lines total; focused, testable, and shippable in <2h.

## 3. Implementation

### `frontend/src/lib/api/file-manifest.ts`

```ts
// frontend/src/lib/api/file-manifest.ts
// Lightweight manifest: list parquet files for (repo, dateFolder) with CDN URLs.
// Strategy:
// - Prefer cached manifest (memory + localStorage)
// - If missing and HF_TOKEN present, fetch via authenticated list_repo_tree (single call)
// - If no token or 429, construct best-effort CDN URLs from last-known pattern
// - All data fetches use public CDN URLs to avoid auth rate limits

const CDN_ROOT = 'https://huggingface.co/datasets';
const MANIFEST_TTL_MS = 1000 * 60 * 30; // 30m

interface FileEntry {
  path: string;        // repo-relative path, e.g. "enriched/2026-04-29/slug.parquet"
  cdnUrl: string;      // public CDN URL
  size?: number;       // optional, from tree entry
}

type Manifest = FileEntry[];

type TreeItem = {
  path: string;
  type: 'file' | 'dir';
  size?: number;
};

type RepoParts = { repo: string; dateFolder: string };

function buildCacheKey({ repo, dateFolder }: RepoParts): string {
  return `vanguard:manifest:${repo}:${dateFolder}`;
}

function buildCdnUrl(repo: string, path: string): string {
  // Public CDN — no Authorization header required
  return `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(path)}`;
}

function loadFromLocalStorage(key: string): Manifest | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { ts: number; manifest: Manifest };
    if (Date.now() - parsed.ts > MANIFEST_TTL_MS) {
      localStorage.removeItem(key);
      return null;
    }
    return parsed.manifest;
  } catch {
    return null;
  }
}

function saveToLocalStorage(key: string, manifest: Manifest): void {
  try {
    localStorage.setItem(key, JSON.stringify({ ts: Date.now(), manifest }));
  } catch {
    // ignore storage quota issues
  }
}

async function fetchTreeWithAuth(repo: string, dateFolder: string): Promise<TreeItem[]> {
  const token = process.env['HF_TOKEN'] || process.env['NEXT_PUBLIC_HF_TOKEN'] || '';
  if (!token) return [];

  // Single non-recursive call per dateFolder to minimize quota use
  const res = await fetch(
    `https://huggingface.co/api/datasets/${encodeURIComponent(repo)}/tree?path=${encodeURIComponent(
      dateFolder
    )}&recursive=false`,
    {
      headers: { Authorization: `Bearer ${token}` },
    }
  );

  if (!res.ok) {
    if (res.status === 429) {
      // Let caller handle fallback
      throw new Error('HF_API_RATE_LIMIT');
    }
    throw new Error(`HF_API_ERROR: ${res.status}`);
  }

  return (await res.json()) as TreeItem[];
}

function inferParquetFilesFromTree(tree: TreeItem[], dateFolder: string): string[] {
  // Expect enriched/ or batches/ mirror outputs; pick .parquet files
  return tree
    .filter((t) => t.type === 'file' && t.path.endsWith('.parquet'))
    .map((t) => t.path)
    .concat(
      // Fallback pattern: if tree empty, assume common output path pattern
      tree.length === 0 ? [`enriched/${dateFolder}/*.parquet`] : []
    );
}

export async function getFileManifest({
  repo,
  dateFolder,
  options = { skipCache: false },
}: RepoParts & { options?: { skipCache?: boolean } }): Promise<Manifest> {
  const key = buildCacheKey({ repo, dateFolder });
  if (!options.skipCache) {
    const cached = loadFromLocalStorage(key);
    if (cached) return cached;
  }

  let candidates: string[] = [];

  try {
    const tree = await fetchTreeWithAuth(repo, dateFolder);
    candidates = inferParquetFilesFromTree(tree, dateFolder);
  } catch (err: any) {
    // On 429 or missing token, use best-effort pattern so UI remains usable
    // (training jobs typically produce enriched/YYYY-MM-DD/*.parquet)
    candidates = [`enriched/${dateFolder}/*.parquet`, `batches/mirror-merged/${dateFolder}/*.parquet`];
  }

  // De-duplicate and build manifest with CDN URLs
  const seen = new Set<string>();
  const manifest: Manifest = [];

  for (const pattern of candidates) {
    // For exact paths from tree, use as-is; for patterns, we cannot list contents without API.
    // We'll expose the pattern as a single entry so callers can attempt range requests or show UI hint.
    if (seen.has(pattern)) continue;
    seen.add(pattern);

    manifest.push({
      path: pattern,
      cdnUrl: buildCdnUrl(repo, pattern),
    });
  }

  // If we got real file paths from tree, prefer them
  const exactFiles = candidates.filter((c) => !c.includes('*'));
  if (exactFiles.length > 0) {
    // Replace manifest with exact files only
    const exactManifest: Manifest = exactFiles.map((path) => ({
      path,
      cdnUrl: buildCdnUrl(repo, path),
    }));
    saveToLocalStorage(key, exactManifest);
    return exactManifest;
  }

  saveToLocalStorage(key, manifest);
  return manifest;
}

export function clearManifestCache(repo?: string, dateFolder?: string): void {
  try {
    if (!repo) {
      Object.keys(localStorage)
        .filter((k) => k.startsWith('vanguard:manifest:'))
        .forEach((k) => localStorage.removeItem(k));
      return;
    }
    if (!dateFolder) {
      Object.keys(localStorage)
        .filter((k) => k.startsWith(`vanguard:manifest:${repo}:`))
        .forEach((k) => localStorage.removeItem(k));
      return;
    }
    localStorage.removeItem(buildCacheKey({ repo, dateFolder }));
  } catch {
    // ignore
  }
}
```

### `frontend/src/lib/api/data.ts` (refactor)

```ts
// frontend/src/lib/api/data.ts
// Refactored to use CDN URLs and manifest cache.

import { getFileManifest, clearManifestCache } from './file-manifest';

const DEFAULT_REPO = 'your-org/vanguard-datasets'; // adjust per env

export interface DatasetFile {
  name: string;
  cdnUrl: string;
  size?: number;
}

export async function listDatasetFiles(dateFolder: string): Promise<DatasetFile[]> {
  const manifest = await getFileManifest({ repo: DEFAULT_REPO, dateFolder });
  return manifest.map((f) => ({
    name: f.path.split('/').pop() || f.path,
    cdnUrl: f.cdnUrl,
    size: f.size,
  }));
}

export async function getLatestFile(dateFolder
