# vanguard / frontend

## 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every frontend training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Frontend likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repo schemas, causing `pyarrow.CastError` and wasted cycles.
- Missing CDN-bypass strategy: training/data loads still route through `/api/` endpoints instead of public CDN URLs (`/resolve/main/`), needlessly consuming rate limits.
- No file-list caching layer in the frontend build/runtime: repeated dev/preview runs re-query HF instead of reading a static JSON artifact.
- No deterministic repo selection for commit-cap mitigation: writes could collide across runs; no hash-slug → sibling repo routing.

## 2. Proposed change

Add a frontend build-time manifest generator + CDN-only data loader for vanguard:

- File: `/opt/axentx/vanguard/src/lib/data/fileList.ts` (new) — exports typed manifest and helper to fetch via CDN.
- File: `/opt/axentx/vanguard/src/lib/data/generateManifest.ts` (new) — Node script run during build (or manually) that calls HF API **once** per `(repo, dateFolder)`, saves `manifest-{repo}-{date}.json` to `/static/data/`.
- File: `/opt/axentx/vanguard/src/lib/data/parquetLoader.ts` (new) — uses CDN URLs + streaming `fetch` + `apache-arrow` (or `parquet-wasm`) to project `{prompt, response}` only; avoids `load_dataset`.
- Update build script / `package.json` script: `"build:manifest": "node --loader ts-node/esm src/lib/data/generateManifest.ts"`.

Scope: ~120–180 lines total; focused, testable, and <2h.

## 3. Implementation

```bash
# Ensure project root
cd /opt/axentx/vanguard
```

### 3.1 Install deps (if not present)

```bash
npm install apache-arrow parquet-wasm
npm install -D @types/node ts-node
```

### 3.2 Create types and manifest generator

`src/lib/data/fileList.ts`

```ts
export interface FileEntry {
  path: string;
  size: number;
  sha: string;
  cdnUrl: string;
}

export interface RepoDateManifest {
  repo: string;
  dateFolder: string;
  generatedAt: string;
  files: FileEntry[];
}

export function cdnUrl(repo: string, path: string): string {
  // Public CDN — no auth
  return `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;
}
```

`src/lib/data/generateManifest.ts`

```ts
import { writeFileSync, mkdirSync } from 'fs';
import { resolve } from 'path';
import { HfApi } from 'huggingface.js'; // or use fetch directly
import { RepoDateManifest, cdnUrl } from './fileList';

const HF_TOKEN = process.env.HF_TOKEN || '';
const api = new HfApi({ token: HF_TOKEN });

async function listRepoFolder(repo: string, folder: string) {
  // list_repo_tree(path, recursive=False) equivalent
  const tree = await api.listRepoTree({ repo, path: folder, recursive: false });
  return tree;
}

async function generate(repo: string, dateFolder: string) {
  const entries = await listRepoFolder(repo, dateFolder);
  const files = entries
    .filter((e) => e.type === 'file' && e.path.endsWith('.parquet'))
    .map((e) => ({
      path: e.path,
      size: e.size || 0,
      sha: e.sha || '',
      cdnUrl: cdnUrl(repo, e.path),
    }));

  const manifest: RepoDateManifest = {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files,
  };

  const outDir = resolve('static/data');
  mkdirSync(outDir, { recursive: true });
  const outPath = resolve(outDir, `manifest-${repo.replace(/\//g, '_')}-${dateFolder}.json`);
  writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written: ${outPath} (${files.length} files)`);
}

// CLI: node generateManifest.js <repo> <dateFolder>
const [repo, dateFolder] = process.argv.slice(2);
if (!repo || !dateFolder) {
  console.error('Usage: node generateManifest.js <repo> <dateFolder>');
  process.exit(1);
}

generate(repo, dateFolder).catch((err) => {
  console.error(err);
  process.exit(1);
});
```

### 3.3 CDN-only Parquet loader (project {prompt, response})

`src/lib/data/parquetLoader.ts`

```ts
import { Table } from 'apache-arrow';
import { readParquet } from 'parquet-wasm/bundler/arrow1';

export interface Sample {
  prompt: string;
  response: string;
}

export async function loadParquetSamples(cdnUrl: string): Promise<Sample[]> {
  const res = await fetch(cdnUrl);
  const buffer = await res.arrayBuffer();
  const table = readParquet(new Uint8Array(buffer)) as Table;
  const prompts = table.getChild('prompt')?.toArray() || [];
  const responses = table.getChild('response')?.toArray() || [];

  const samples: Sample[] = [];
  for (let i = 0; i < Math.min(prompts.length, responses.length); i++) {
    const p = prompts[i];
    const r = responses[i];
    if (typeof p === 'string' && typeof r === 'string') {
      samples.push({ prompt: p, response: r });
    }
  }
  return samples;
}

export async function loadManifestSamples(manifestPath: string): Promise<Sample[]> {
  const res = await fetch(manifestPath);
  const manifest = (await res.json()) as { files: Array<{ cdnUrl: string }> };
  const all: Sample[] = [];
  for (const f of manifest.files) {
    const samples = await loadParquetSamples(f.cdnUrl);
    all.push(...samples);
  }
  return all;
}
```

### 3.4 Add build script

`package.json` (add)

```json
"scripts": {
  "build:manifest": "node --loader ts-node/esm src/lib/data/generateManifest.ts"
}
```

Usage (run once per date folder, or in CI):

```bash
HF_TOKEN=hf_xxx npm run build:manifest -- my-org/my-dataset 2024-05-01
```

Then in frontend code:

```ts
import { loadManifestSamples } from '$lib/data/parquetLoader';
const samples = await loadManifestSamples('/data/manifest-my-org_my-dataset-2024-05-01.json');
```

## 4. Verification

1. Run manifest generation:
   ```bash
   HF_TOKEN=hf_xxx npm run build:manifest -- my-org/my-dataset 2024-05-01
   ```
   Confirm `static/data/manifest-*.json` exists and lists `.parquet` files with valid `cdnUrl`s.

2. Start dev server and load a route that uses `loadManifestSamples`. Open browser devtools Network tab:
   - Verify requests go to `https://huggingface.co/datasets/.../resolve/main/...` (CDN).
   - Confirm **no** requests to `https://huggingface.co/api/...` during data fetch.

3. Check sample projection:
   - Console-log first few samples; confirm only `{prompt, response}` fields present and are strings.

4. Rate-limit safety:
   - Re-run manifest generation within 60s — it should still succeed (single API call). If you hit 429, wait 360s and retry (per HF API policy). After manifest is saved, **zero** authenticated API calls occur during frontend training/data browsing.
