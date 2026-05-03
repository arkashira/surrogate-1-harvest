# vanguard / frontend

## 1. Diagnosis

- Frontend has no deterministic file-list snapshot for dataset folders → training/frontend re-enumerates repos at runtime and triggers HF API 429s.
- No content-addressed manifest (file list + SHA256) → CDN-only fetches impossible; epochs non-reproducible and resume/restart unsafe.
- Missing frontend utility to embed and validate a pre-listed file manifest → forces runtime HF API calls during data-load or preview flows.
- No lightweight manifest generator for date-folders → ingestion/training must call `list_repo_tree` repeatedly instead of once per folder.
- No frontend-side fallback to CDN URLs when manifest is present → can’t bypass `/api/` auth checks and rate limits.

## 2. Proposed change

Add a frontend-side manifest module and a one-shot CLI generator:

- `vanguard/frontend/src/lib/manifest.ts` — types + loader + CDN URL builder + integrity validator
- `vanguard/frontend/src/lib/__tests__/manifest.test.ts` — unit tests for parsing and CDN URL construction
- `vanguard/frontend/scripts/generate-manifest.js` — Node CLI: `node generate-manifest.js --repo <repo> --path <date-folder> --out <manifest.json>`
- Update `vanguard/frontend/src/config/datasets.ts` to import/use the manifest for CDN-only mode.

Scope: frontend-only; no backend or training changes. Deliverable is a <2h incremental improvement that enables deterministic CDN fetches from the frontend.

## 3. Implementation

### 3.1 Frontend manifest loader (`vanguard/frontend/src/lib/manifest.ts`)

```ts
// vanguard/frontend/src/lib/manifest.ts

export interface FileEntry {
  path: string;        // relative to repo root or dataset root
  size: number;
  sha256?: string;
  // optional etag/lastModified if you want stronger caching hints
}

export interface DatasetManifest {
  repo: string;        // e.g. "datasets/myorg/myrepo"
  folder: string;      // e.g. "batches/mirror-merged/2026-04-29"
  generatedAt: string; // ISO timestamp
  files: FileEntry[];
  // optional aggregate hash for quick mismatch detection
  manifestSha256?: string;
}

const CDN_ROOT = 'https://huggingface.co';

export function buildCdnUrl(repo: string, filePath: string): string {
  // Public CDN path — no Authorization header required
  // repo can be "datasets/owner/name" or "owner/name"
  const normalized = repo.replace(/^datasets\//, '');
  return `${CDN_ROOT}/${normalized}/resolve/main/${encodeURI(filePath)}`;
}

export async function fetchManifest(url: string): Promise<DatasetManifest> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Failed to fetch manifest: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<DatasetManifest>;
}

export function validateManifestIntegrity(manifest: DatasetManifest): boolean {
  // Basic structural validation
  if (!manifest.repo || !manifest.folder || !Array.isArray(manifest.files)) {
    return false;
  }
  return manifest.files.every((f) => typeof f.path === 'string' && typeof f.size === 'number');
}

export function filterByExtension(files: FileEntry[], exts: string[]): FileEntry[] {
  const set = new Set(exts.map((e) => e.toLowerCase()));
  return files.filter((f) => set.has(f.path.slice(f.path.lastIndexOf('.')).toLowerCase()));
}

export function toCdnFileList(manifest: DatasetManifest): Array<{ path: string; url: string; size: number }> {
  return manifest.files.map((f) => ({
    path: f.path,
    url: buildCdnUrl(manifest.repo, f.path),
    size: f.size,
  }));
}
```

### 3.2 Unit tests (`vanguard/frontend/src/lib/__tests__/manifest.test.ts`)

```ts
// vanguard/frontend/src/lib/__tests__/manifest.test.ts
import { buildCdnUrl, validateManifestIntegrity, filterByExtension, toCdnFileList } from '../manifest';

describe('manifest utils', () => {
  const mockManifest = {
    repo: 'datasets/myorg/myrepo',
    folder: 'batches/mirror-merged/2026-04-29',
    generatedAt: new Date().toISOString(),
    files: [
      { path: 'batches/mirror-merged/2026-04-29/a.parquet', size: 1024, sha256: 'abc' },
      { path: 'batches/mirror-merged/2026-04-29/b.jsonl', size: 2048 },
    ],
  };

  test('buildCdnUrl normalizes repo and encodes path', () => {
    expect(buildCdnUrl('datasets/myorg/myrepo', 'folder/file.parquet')).toBe(
      'https://huggingface.co/myorg/myrepo/resolve/main/folder/file.parquet'
    );
  });

  test('validateManifestIntegrity accepts valid manifest', () => {
    expect(validateManifestIntegrity(mockManifest)).toBe(true);
  });

  test('filterByExtension filters correctly', () => {
    const filtered = filterByExtension(mockManifest.files, ['.parquet']);
    expect(filtered).toHaveLength(1);
    expect(filtered[0].path).toContain('.parquet');
  });

  test('toCdnFileList maps to CDN URLs', () => {
    const list = toCdnFileList(mockManifest);
    expect(list).toHaveLength(2);
    expect(list[0].url).toContain('resolve/main');
  });
});
```

### 3.3 CLI generator (`vanguard/frontend/scripts/generate-manifest.js`)

```js
#!/usr/bin/env node
// vanguard/frontend/scripts/generate-manifest.js
// Usage: node generate-manifest.js --repo datasets/owner/name --path batches/mirror-merged/2026-04-29 --out manifest.json
// Requires: HUGGING_FACE_TOKEN in env for private repos (public repos work without token)

import { program } from 'commander';
import { huggingface } from '@huggingface/huggingface';
import fs from 'fs';
import crypto from 'crypto';
import { fileURLToPath } from 'url';
import path from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

program
  .name('generate-manifest')
  .description('Generate a content-addressed file manifest for a dataset folder (CDN-only friendly)')
  .requiredOption('--repo <repo>', 'Hugging Face repo (e.g. datasets/owner/name or owner/name)')
  .requiredOption('--path <path>', 'Folder path inside repo (e.g. batches/mirror-merged/2026-04-29)')
  .option('--out <file>', 'Output JSON file', 'manifest.json')
  .option('--no-sha', 'Skip SHA256 computation (faster, no integrity)')
  .parse();

const opts = program.opts();

async function listTree(repo, folderPath) {
  // Normalize repo for HF client
  const repoId = repo.replace(/^datasets\//, '');
  const client = huggingface({ token: process.env.HUGGING_FACE_TOKEN });
  // Use non-recursive listing per immediate folder to minimize API calls/pagination
  // We'll do a recursive=false listing for the target folder; if nested subfolders exist we list them separately as needed.
  // For simplicity and to avoid 429, we list once for the folder and include subfolders by walking locally.
  const listed = await client.listRepoTree({
    repoId,
    path: folderPath,
    recursive: false,
  });

  // If listed is an object with 'files' and 'dirs', collect recursively with minimal calls
  const files = [];
  if (listed && Array.isArray(listed)) {
    // Some clients return array of entries
    for (const entry of listed) {
      if (entry.type === 'file') files.push(entry);
    }
  } else if (listed && listed.files) {
    for (const f of listed.files) if (f.type === 'file') files
