# vanguard / frontend

## 1. Diagnosis
- No persisted manifest per `(repo, dateFolder)` → every training launch re-enumerates via authenticated HF API, burning quota and risking 429.
- Frontend cannot pre-flight or cache available files; users pick invalid/mismatched paths and training fails late.
- Training script uses `load_dataset(streaming=True)` on heterogeneous repos → `pyarrow.CastError` on mixed schemas.
- Lightning Studio is recreated on each run instead of reused → quota waste (~80hr/mo) and slower iteration.
- No CDN-only fallback for dataset files → all fetches go through rate-limited `/api/` endpoints.

## 2. Proposed change
Create a lightweight frontend manifest cache and training launcher that:
- Persists a `manifests/` JSON per `(repo, dateFolder)` after a single authenticated `list_repo_tree` call.
- Embeds the manifest in the training UI so users pick valid files and training uses CDN-only URLs.
- Reuses running Lightning Studio instances and falls back to `lightning-public-prod` (L40S) if H200 unavailable.
- Projects heterogeneous files to `{prompt, response}` at parse time instead of relying on HF streaming loader.

Scope:
- Add `/opt/axentx/vanguard/src/frontend/src/lib/manifest.ts`
- Add `/opt/axentx/vanguard/src/frontend/src/lib/training.ts`
- Update `/opt/axentx/vanguard/src/frontend/src/routes/+page.svelte` (or equivalent) to use manifest cache and launch training.
- Add `/opt/axentx/vanguard/src/frontend/src/lib/lightning.ts` for studio reuse logic.

## 3. Implementation

### manifest.ts
```ts
// src/lib/manifest.ts
import { writable } from 'svelte/store';

export interface ManifestEntry {
  path: string;
  size: number;
  sha: string;
  type: 'file' | 'dir';
}

export interface RepoManifest {
  repo: string;
  folder: string; // e.g. "batches/mirror-merged/2026-04-29"
  generatedAt: string;
  files: ManifestEntry[];
  cdnBase: string; // https://huggingface.co/datasets/{repo}/resolve/main/{folder}
}

const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 1 day

function cacheKey(repo: string, folder: string) {
  return `manifest:${repo}:${folder}`;
}

export async function getOrFetchManifest(
  repo: string,
  folder: string,
  hfToken?: string
): Promise<RepoManifest> {
  const key = cacheKey(repo, folder);
  const cached = localStorage.getItem(key);
  if (cached) {
    const parsed = JSON.parse(cached) as RepoManifest;
    if (Date.now() - new Date(parsed.generatedAt).getTime() < CACHE_TTL_MS) {
      return parsed;
    }
  }

  // Single tree call (non-recursive per folder) to avoid pagination + rate limit
  const res = await fetch(
    `https://huggingface.co/api/datasets/${repo}/tree?path=${encodeURIComponent(
      folder
    )}&recursive=false`,
    {
      headers: hfToken ? { Authorization: `Bearer ${hfToken}` } : {},
    }
  );

  if (!res.ok) {
    throw new Error(`HF tree API failed: ${res.status} ${await res.text()}`);
  }

  const tree = await res.json();
  const files = (tree as any[]).filter((n) => n.type === 'file') as ManifestEntry[];

  const manifest: RepoManifest = {
    repo,
    folder,
    generatedAt: new Date().toISOString(),
    files,
    cdnBase: `https://huggingface.co/datasets/${repo}/resolve/main/${folder}`,
  };

  localStorage.setItem(key, JSON.stringify(manifest));
  return manifest;
}

export const activeManifest = writable<RepoManifest | null>(null);
```

### training.ts
```ts
// src/lib/training.ts
import { getOrFetchManifest } from './manifest';

// Build CDN-only URLs for files (bypasses HF API auth/rate limits during training)
export function buildCdnUrls(manifest: any, filenames: string[]) {
  return filenames.map((name) => `${manifest.cdnBase}/${encodeURIComponent(name)}`);
}

// Lightweight projection to {prompt,response} at parse time to avoid pyarrow schema issues
export function parseParquetProjection(rawBytes: ArrayBuffer) {
  // In browser we can't parse parquet; this function is intended for the Lightning training script.
  // Frontend only sends CDN URLs and a parse spec.
  return {
    parseSpec: {
      type: 'parquet-projection',
      columns: { prompt: 'prompt', response: 'response' },
      dropOtherColumns: true,
    },
  };
}
```

### lightning.ts
```ts
// src/lib/lightning.ts
import { Lightning } from '@lightningai/sdk'; // adjust import per actual SDK

export async function getOrCreateStudio(name: string, machine = 'L40S') {
  const teamspace = await Lightning.Teamspace.current();
  const existing = await teamspace.studios();

  const running = existing.find(
    (s) => s.name === name && s.status === 'Running'
  );
  if (running) return running;

  // Try preferred machine, fall back to public tier
  const machines = [machine, 'L40S', 'A10G', 'T4'];
  let lastErr: any = null;
  for (const m of machines) {
    try {
      return await teamspace.createStudio({
        name,
        machine: m,
        createOk: true,
      });
    } catch (err) {
      lastErr = err;
      continue;
    }
  }
  throw lastErr || new Error('No available machine');
}

export async function runTrainingOnStudio(
  studio: any,
  scriptPath: string,
  args: Record<string, string>
) {
  if (studio.status !== 'Running') {
    // restart if stopped/idle-killed
    await studio.start({ machine: studio.machine });
  }
  return studio.run(scriptPath, { args });
}
```

### +page.svelte (or equivalent)
```svelte
<script lang="ts">
  import { getOrFetchManifest } from '$lib/manifest';
  import { buildCdnUrls } from '$lib/training';
  import { getOrCreateStudio, runTrainingOnStudio } from '$lib/lightning';
  import { activeManifest } from '$lib/manifest';

  let repo = 'your-org/your-dataset';
  let folder = 'batches/mirror-merged/2026-04-29';
  let files: string[] = [];
  let manifest: any = null;
  let loading = false;
  let launchStatus = '';

  async function loadManifest() {
    loading = true;
    try {
      manifest = await getOrFetchManifest(repo, folder, import.meta.env.VITE_HF_TOKEN);
      files = manifest.files.map((f: any) => f.path);
      activeManifest.set(manifest);
    } catch (e) {
      alert('Failed to load manifest: ' + e);
    } finally {
      loading = false;
    }
  }

  async function launchTraining() {
    if (!manifest || files.length === 0) return;
    try {
      launchStatus = 'Preparing studio...';
      const studio = await getOrCreateStudio('vanguard-training');
      launchStatus = 'Submitting training...';

      const urls = buildCdnUrls(manifest, files);
      await runTrainingOnStudio(studio, 'train.py', {
        file_urls: JSON.stringify(urls),
        parse_spec: JSON.stringify({ type: 'parquet-projection', columns: { prompt: 'prompt', response: 'response' } }),
      });

      launchStatus = 'Training submitted (check Lightning Studio logs)';
    } catch (e) {
      launchStatus = 'Launch failed: ' + e;
    }
  }

  // load on mount
  loadManifest();
</script>

<main>
  <h1>Vanguard Training Launcher</h1>

 
