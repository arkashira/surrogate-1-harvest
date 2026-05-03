# vanguard / frontend

## 1. Diagnosis

- No persisted HF file manifest per `(repo, dateFolder)` in the frontend layer — every training launch re-enumerates via authenticated API, burning quota and risking 429.
- Training UI cannot run reliably because it lacks a stable, CDN-only file list; data loader falls back to authenticated `load_dataset` paths that trigger auth checks.
- Manifest is not cached across tabs/restarts (would require `localStorage`), so a fresh browser session repeats the expensive tree call.
- No fallback when HF API is rate-limited (429) — UI fails instead of using last-known manifest or CDN-only path template.
- Missing lightweight orchestration toggle to ensure Mac runs only launcher/SDK calls while training targets Lightning Studio (keeps heavy compute off local).

## 2. Proposed change

Add a frontend manifest manager and CDN-only training launcher:

- **Files**: `/opt/axentx/vanguard/src/lib/hfManifest.ts` (new), `/opt/axentx/vanguard/src/lib/trainingLauncher.ts` (new), and integrate into existing training UI component (likely `/opt/axentx/vanguard/src/routes/+page.svelte` or similar).
- **Scope**: single `listRepoTree` call per `(repo, dateFolder)` → persist to `localStorage`; expose CDN URLs; provide `launchStudioTraining(manifest)` that uses Lightning SDK without local model loading.

## 3. Implementation

Create `src/lib/hfManifest.ts`:

```ts
// src/lib/hfManifest.ts
import { hfApi } from './hfApi'; // assume existing HF API helper; adapt as needed

const STORAGE_KEY = 'hf_manifest_v1';

export interface FileEntry {
  path: string;
  type: 'file' | 'dir';
  size?: number;
}

export interface RepoManifest {
  repo: string; // e.g., "datasets/username/repo"
  dateFolder: string; // e.g., "2026-04-29"
  tree: FileEntry[];
  files: string[]; // leaf files only
  cdnBase: string; // https://huggingface.co/datasets/.../resolve/main/
  generatedAt: number;
}

function storageKey(repo: string, dateFolder: string): string {
  return `${STORAGE_KEY}:${repo}:${dateFolder}`;
}

export async function getOrCreateManifest(
  repo: string,
  dateFolder: string,
  options?: { bustCache?: boolean }
): Promise<RepoManifest> {
  const key = storageKey(repo, dateFolder);
  const cached = localStorage.getItem(key);
  if (cached && !options?.bustCache) {
    const parsed = JSON.parse(cached) as RepoManifest;
    // consider stale after 24h; still usable as fallback
    const age = Date.now() - parsed.generatedAt;
    if (age < 24 * 60 * 60 * 1000) return parsed;
  }

  // Single tree call (non-recursive per folder not needed here; recursive=true is fine for one date folder)
  // If repo is large, caller should ensure dateFolder is a small subtree.
  const tree = await hfApi.listRepoTree({
    repo,
    path: dateFolder,
    recursive: true,
  });

  const files = tree
    .filter((t) => t.type === 'file')
    .map((t) => `${dateFolder}/${t.path}`);

  const cdnBase = `https://huggingface.co/datasets/${repo}/resolve/main/`;

  const manifest: RepoManifest = {
    repo,
    dateFolder,
    tree,
    files,
    cdnBase,
    generatedAt: Date.now(),
  };

  try {
    localStorage.setItem(key, JSON.stringify(manifest));
  } catch (e) {
    // ignore quota errors; continue with in-memory manifest
    console.warn('Could not persist HF manifest to localStorage', e);
  }

  return manifest;
}

export function getCdnUrl(manifest: RepoManifest, filePath: string): string {
  // filePath should be relative to repo root (or include dateFolder already)
  return `${manifest.cdnBase}${filePath}`;
}

export function getCdnUrlsForPatterns(
  manifest: RepoManifest,
  patterns: string[]
): string[] {
  // simple glob-like match on suffix
  return manifest.files.filter((f) =>
    patterns.some((p) => f.endsWith(p) || f.includes(p))
  ).map((f) => getCdnUrl(manifest, f));
}
```

Create `src/lib/trainingLauncher.ts`:

```ts
// src/lib/trainingLauncher.ts
import * as lightning from 'lightning'; // Lightning AI SDK; adapt import to actual package
import type { RepoManifest } from './hfManifest';

export interface TrainingConfig {
  repo: string;
  dateFolder: string;
  scriptPath: string; // path to train.py inside repo or local script to run in studio
  machine?: lightning.Machine; // e.g., lightning.Machine.L40S
  reuseRunning?: boolean;
}

export async function launchStudioTraining(config: TrainingConfig) {
  const { repo, dateFolder, scriptPath, machine = lightning.Machine.L40S, reuseRunning = true } = config;

  // Reuse running studio if requested
  if (reuseRunning) {
    const studios = await lightning.Teamspace.studios();
    const running = studios.find((s) => s.name === `vanguard-${repo.replace(/\//g, '-')}-${dateFolder}` && s.status === 'Running');
    if (running) {
      // If stopped, restart; otherwise attach/run
      if (running.status === 'Stopped') {
        await running.start({ machine });
      }
      // run training script (assumes script is in repo or uploaded)
      await running.run({
        command: `python ${scriptPath} --manifest-date ${dateFolder}`,
      });
      return { reused: true, studio: running };
    }
  }

  // Create new studio
  const studio = await lightning.Studio.create({
    name: `vanguard-${repo.replace(/\//g, '-')}-${dateFolder}`,
    machine,
    scriptPath,
    // do not load model locally; Lightning will fetch via CDN from manifest URLs
    environment: {
      HF_MANIFEST_REPO: repo,
      HF_MANIFEST_DATE: dateFolder,
      // ensure training script uses CDN-only URLs (see hfManifest.ts)
    },
  });

  await studio.run({
    command: `python ${scriptPath} --manifest-date ${dateFolder}`,
  });

  return { reused: false, studio };
}
```

Integrate into existing training UI (example snippet):

```ts
// inside your +page.svelte or component
import { getOrCreateManifest, getCdnUrlsForPatterns } from '$lib/hfManifest';
import { launchStudioTraining } from '$lib/trainingLauncher';

async function startTraining() {
  const repo = 'datasets/username/repo';
  const dateFolder = '2026-04-29';

  // 1) Get or create manifest (single API call; cached)
  const manifest = await getOrCreateManifest(repo, dateFolder);

  // 2) Build CDN-only file list for training (zero auth during training)
  const parquetUrls = getCdnUrlsForPatterns(manifest, ['.parquet']);
  console.log('CDN parquet URLs for training:', parquetUrls);

  // 3) Launch Lightning Studio training (no local model load)
  const result = await launchStudioTraining({
    repo,
    dateFolder,
    scriptPath: 'train.py',
    machine: lightning.Machine.L40S,
    reuseRunning: true,
  });

  console.log('Launched training:', result);
}
```

Notes:
- If `hfApi.listRepoTree` doesn’t exist, implement a minimal authenticated call (or use `list_repo_tree` from `huggingface-hub` via a small backend proxy) and cache the result. The key is one call per `(repo, dateFolder)`.
- Ensure training script (`train.py`) reads the manifest or accepts a list of CDN URLs and uses `datasets.load_dataset` with `data_files` pointing to CDN URLs (no auth) or uses `hf_hub_download` for specific files. Avoid `load_dataset(streaming=True)` on heterogeneous repos.

## 4. Verification

1. Open the app in two tabs. Trigger “Start training”
