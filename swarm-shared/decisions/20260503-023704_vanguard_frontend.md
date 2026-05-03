# vanguard / frontend

## 1. Diagnosis

- Frontend data layer still likely calls authenticated Hugging Face API (`/api/datasets/...` or `list_repo_tree`) during runtime → burns quota and exposes 429 risk on user machines.
- No static file manifest embedded in the frontend bundle → every session re-enumerates repo files instead of using CDN-only fetches.
- Missing build-time step to generate and inline a `file-manifest.json` for the current dataset snapshot (date/slug) so training/preview can use `https://huggingface.co/datasets/.../resolve/main/...` without Authorization.
- No fallback behavior when HF API fails (429/5xx) — frontend should silently switch to CDN URLs if available.
- No TypeScript types or utility to reliably construct CDN URLs from repo/slug/path (error-prone string concat).

## 2. Proposed change

- Add a build-time script: `scripts/generate-manifest.ts` that runs during `npm run build` (or `vite build`) and emits `src/assets/manifest.json`.
- Add a runtime module: `src/lib/hf-cdn.ts` with typed helpers to resolve CDN URLs and a fetcher that prefers CDN and falls back to API.
- Wire into Vite: a simple plugin or `vite.config.ts` hook that runs the manifest generator before build.
- Update any frontend data-loading code (likely in `src/lib/data.ts` or similar) to use the manifest + CDN fetcher.

## 3. Implementation

Below are minimal, copy-paste-ready additions. Adjust import paths if your structure differs.

### 3.1 Add TypeScript types and utilities

`src/lib/hf-cdn.ts`
```ts
// Typed helpers to use HuggingFace CDN and avoid authenticated API calls.
// CDN pattern: https://huggingface.co/datasets/{repo}/resolve/main/{path}

export interface ManifestFile {
  repo: string;       // e.g. "username/dataset"
  path: string;       // relative path in repo, e.g. "batches/mirror-merged/2026-05-03/slug.parquet"
  sha?: string;       // optional integrity
  size?: number;
}

export interface Manifest {
  generatedAt: string; // ISO
  repo: string;
  root: string;        // optional root folder inside repo
  files: ManifestFile[];
}

export function cdnUrl(repo: string, path: string): string {
  const cleanRepo = repo.replace(/^\/+/, '').replace(/\/+$/, '');
  const cleanPath = path.replace(/^\/+/, '');
  return `https://huggingface.co/datasets/${cleanRepo}/resolve/main/${cleanPath}`;
}

export async function fetchViaCdn(
  repo: string,
  path: string,
  options?: RequestInit & { preferCdn?: boolean }
): Promise<Response> {
  const preferCdn = options?.preferCdn ?? true;
  const url = cdnUrl(repo, path);

  if (preferCdn) {
    try {
      const res = await fetch(url, { ...options, cache: 'no-cache' });
      // CDN returns 200 for public files; 404/403 if missing or private
      if (res.ok) return res;
    } catch (e) {
      // network error — fall through to API fallback if allowed
    }
  }

  // Fallback to authenticated API (only if caller explicitly allows)
  // Note: frontend should avoid this path in production; keep for dev fallback.
  const apiUrl = `https://huggingface.co/api/datasets/${repo}/revision/main?path=${encodeURIComponent(path)}`;
  return fetch(apiUrl, { ...options, cache: 'no-cache' });
}

export async function loadManifest(): Promise<Manifest | null> {
  try {
    // Vite will copy/manifest.json into assets; adjust path if needed.
    const res = await fetch('/assets/manifest.json', { cache: 'no-cache' });
    if (!res.ok) return null;
    return res.json() as Promise<Manifest>;
  } catch {
    return null;
  }
}
```

### 3.2 Add build-time manifest generator

`scripts/generate-manifest.ts`
```ts
#!/usr/bin/env tsx
// Generates a static manifest for the dataset snapshot so the frontend can use CDN-only fetches.
// Run: `npm run generate:manifest` (or wire into prebuild)

import { writeFileSync, mkdirSync } from 'fs';
import { resolve } from 'path';
import { listRepoTree } from 'huggingface-hub'; // or use direct REST calls if preferred

const REPO = process.env.HF_DATASET_REPO || 'username/dataset';
const ROOT = process.env.HF_DATASET_ROOT || ''; // e.g. "batches/mirror-merged/2026-05-03"
const OUT_DIR = resolve(process.cwd(), 'src/assets');
const OUT_FILE = resolve(OUT_DIR, 'manifest.json');

async function run() {
  try {
    // listRepoTree with recursive=false per folder strategy (avoid huge recursive calls)
    // If you have nested folders, call per subfolder and flatten.
    const tree = await listRepoTree(REPO, {
      path: ROOT || undefined,
      recursive: false,
    });

    // If root is a folder, you may want to recurse one level or use a saved file list.
    // For simplicity, this collects files at this level only.
    const files = tree
      .filter((t) => t.type === 'file')
      .map((t) => ({
        repo: REPO,
        path: ROOT ? `${ROOT.replace(/\/+$/, '')}/${t.path.replace(/^\/+/, '')}` : t.path,
        size: t.size,
        sha: t.oid,
      }));

    const manifest = {
      generatedAt: new Date().toISOString(),
      repo: REPO,
      root: ROOT || undefined,
      files,
    };

    mkdirSync(OUT_DIR, { recursive: true });
    writeFileSync(OUT_FILE, JSON.stringify(manifest, null, 2));
    console.log(`Manifest written to ${OUT_FILE} (${files.length} files)`);
  } catch (err) {
    console.error('Failed to generate manifest:', err);
    process.exit(1);
  }
}

run();
```

Make it executable and add to package.json:

`package.json` (additions)
```json
{
  "scripts": {
    "generate:manifest": "tsx scripts/generate-manifest.ts",
    "prebuild": "npm run generate:manifest",
    "build": "vite build"
  }
}
```

If you prefer not to depend on `huggingface-hub` in the frontend repo, replace the generator with a one-off curl-based script that uses the public tree API and saves the JSON — the key is to run it on your Mac (or CI) and commit/embed the manifest so the frontend never calls the API at runtime.

### 3.3 Wire into your data-loading layer

Example update to your existing loader (adjust to your actual file):

`src/lib/data.ts` (or wherever you load dataset files)
```ts
import { loadManifest, fetchViaCdn } from './hf-cdn';

let manifestPromise: Promise<any> | null = null;

export async function getDatasetFile(filename: string) {
  if (!manifestPromise) manifestPromise = loadManifest();
  const manifest = await manifestPromise;

  const entry = manifest?.files.find((f) => f.path.endsWith(filename) || f.path.includes(filename));
  if (!entry) {
    throw new Error(`File ${filename} not found in manifest`);
  }

  // Use CDN-first fetch (no Authorization header)
  return fetchViaCdn(entry.repo, entry.path, { preferCdn: true });
}
```

### 3.4 Vite asset handling (if needed)

Vite will copy `src/assets/manifest.json` into the build output automatically. If you want to inline it as an import, you can import it as JSON in code with a `?url` or raw import depending on your Vite config. The fetch-based approach above is safest for dev/prod parity.

## 4. Verification

1. Run generation locally:
   ```bash
   npm run generate:manifest
   ```
   Confirm `src/assets/manifest.json` exists and contains expected file entries.

2. Start dev server:
   ```bash
   npm run dev
   ```
   Open
