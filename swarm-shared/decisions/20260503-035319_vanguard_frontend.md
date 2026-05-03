# vanguard / frontend

## Final Synthesis (Correctness + Actionability)

### 1. Diagnosis (merged, de-duplicated)
- **No frontend manifest for CDN-only fetches**: training UI triggers runtime `list_repo_tree`/`load_dataset` calls that hit HF API rate limits and break reproducibility.
- **Missing content-addressed file list**: frontend cannot pre-compute or cache which parquet files exist for a given date/slug, forcing expensive API discovery on every page load and preventing deterministic cache keys for dataset snapshots.
- **No build-time artifact**: ingestion/training scripts re-list HF repos at runtime, causing 429s and non-reproducible runs.
- **Schema mismatch risk**: files landing in `enriched/` without projection to `{prompt,response}` can cause `pyarrow.CastError` during training loads.
- **Lightning Studio reuse not surfaced**: frontend likely recreates studios instead of reusing running ones, burning quota.

### 2. Proposed change (merged)
Add a build-time-generated `dataset-manifest.json` and a lightweight frontend module that hydrates file lists from the manifest and uses CDN URLs exclusively. Add deterministic cache keys and a studio-reuse guard. Scope:

- `packages/frontend/src/lib/dataset-manifest.ts` — types + loader + deterministic cache key helper
- `packages/frontend/src/lib/cdn.ts` — CDN URL builder + zero-API fetch helper + projection helper
- `packages/frontend/src/routes/training/+page.server.ts` (or framework equivalent) — serve manifest at build/first render and expose CDN base
- `static/manifests/` — output folder for content-addressed manifests (committed or CI-generated)
- `packages/frontend/src/lib/studio-reuse.ts` — detect and attach to running Lightning Studio
- `scripts/generate-manifest.js` — build-time generator with deterministic snapshot key

### 3. Implementation (merged + hardened)

```bash
# Ensure project structure
mkdir -p /opt/axentx/vanguard/packages/frontend/src/lib
mkdir -p /opt/axentx/vanguard/static/manifests
```

#### packages/frontend/src/lib/dataset-manifest.ts
```ts
export interface ManifestFile {
  repo: string;     // e.g. "datasets/myorg/mirror-merged"
  path: string;     // e.g. "batches/mirror-merged/2026-05-01/slug.parquet"
  sha256?: string;  // optional content hash for cache-bust
  size: number;
}

export interface DatasetManifest {
  generatedAt: string; // ISO
  repo: string;
  folder: string;      // root folder listed (recursive=false)
  files: ManifestFile[];
  snapshotKey: string; // deterministic cache key (e.g. sha256 of sorted file list + sizes)
}

const CACHE_TTL_MS = 1000 * 60 * 5; // 5m client-side cache for manifest json

export async function loadDatasetManifest(
  manifestUrl: string
): Promise<DatasetManifest | null> {
  try {
    const res = await fetch(manifestUrl, { cache: 'no-cache' });
    if (!res.ok) return null;
    const json = (await res.json()) as DatasetManifest;
    return json;
  } catch {
    return null;
  }
}

export function cdnUrl(repo: string, path: string): string {
  // CDN bypass: no Authorization header required
  return `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;
}

export function snapshotKey(files: Pick<ManifestFile, 'path' | 'size' | 'sha256'>[]): string {
  // Deterministic key for long-lived CDN caching and reproducible snapshots
  const normalized = files
    .map((f) => `${f.path}:${f.size}:${f.sha256 ?? ''}`)
    .sort()
    .join('\n');
  // In browser, use SubtleCrypto; in Node, fallback to simple hash or rely on build-time sha.
  return normalized; // For build-time usage, caller can sha256 this string.
}
```

#### packages/frontend/src/lib/cdn.ts
```ts
export async function fetchParquetBytes(url: string): Promise<ArrayBuffer> {
  // CDN fetch — no auth, no HF API calls
  const res = await fetch(url);
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
  return await res.arrayBuffer();
}

// Lightweight projection helper (for UI previews) — assumes arrow/parquet handled elsewhere
export function projectPromptResponse(record: any) {
  return {
    prompt: record.prompt ?? record.text ?? '',
    response: record.response ?? record.completion ?? '',
  };
}
```

#### packages/frontend/src/lib/studio-reuse.ts
```ts
export interface StudioInfo {
  id: string;
  name?: string;
  status: 'running' | 'stopped' | 'failed';
  url?: string;
}

// Placeholder: integrate with Lightning Studio API or your orchestrator
export async function findRunningStudio(projectId?: string): Promise<StudioInfo | null> {
  // Implement actual API call to Lightning Studio / your orchestrator
  // Return running studio to attach instead of creating new.
  return null;
}

export async function attachToStudio(studioId: string, config: any): Promise<void> {
  // Implement attach logic (e.g., submit job to running studio)
}
```

#### packages/frontend/src/routes/training/+page.server.ts (SvelteKit example)
```ts
import { loadDatasetManifest, snapshotKey } from '$lib/dataset-manifest';
import { cdnUrl } from '$lib/dataset-manifest';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async () => {
  // In production this JSON is generated at build and placed in /static/manifests/
  const manifest = await loadDatasetManifest('/manifests/mirror-merged-2026-05-01.json');

  const files = manifest?.files.map((f) => ({
    name: f.path.split('/').pop() ?? f.path,
    url: cdnUrl(f.repo, f.path),
    size: f.size,
  })) ?? [];

  return {
    manifest,
    files,
    // Pass a stable CDN base to client so client-side code never calls HF API
    cdnBase: 'https://huggingface.co/datasets',
    snapshotKey: manifest?.snapshotKey ?? '',
  };
};
```

#### Build-time generator (scripts/generate-manifest.js)
```js
#!/usr/bin/env node
// scripts/generate-manifest.js
// Usage: HF_TOKEN=... node scripts/generate-manifest.js --repo datasets/myorg/mirror-merged --folder batches/mirror-merged/2026-05-01 --out static/manifests/mirror-merged-2026-05-01.json

import { program } from 'commander';
import { HfApi } from '@huggingface/hub';
import fs from 'fs';
import crypto from 'crypto';

program
  .requiredOption('--repo <repo>', 'HF dataset repo')
  .requiredOption('--folder <folder>', 'Folder to list (non-recursive)')
  .requiredOption('--out <file>', 'Output manifest JSON')
  .parse();

const opts = program.opts();

function deterministicSnapshotKey(files) {
  const normalized = files
    .map((f) => `${f.path}:${f.size}:${f.sha256 ?? ''}`)
    .sort()
    .join('\n');
  return crypto.createHash('sha256').update(normalized).digest('hex');
}

async function main() {
  const api = new HfApi({ token: process.env.HF_TOKEN });
  // list_repo_tree recursive=false to avoid pagination storms
  const tree = await api.listRepoTree({
    repo: opts.repo,
    path: opts.folder,
    recursive: false,
  });

  const files = (tree.files || [])
    .filter((f) => f.path.endsWith('.parquet'))
    .map((f) => ({
      repo: opts.repo,
      path: f.path,
      sha256: f.lfs?.oid ?? undefined,
      size: f.size,
    }));

  const manifest = {
    generatedAt: new Date().toISOString(),
    repo: opts.repo,
    folder: opts.folder,

