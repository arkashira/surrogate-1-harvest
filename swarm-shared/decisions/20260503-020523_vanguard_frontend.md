# vanguard / frontend

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: frontend (or orchestrator) re-triggers authenticated `list_repo_tree` on every run, burning HF API quota and risking 429s.
- Training/data loader likely uses `load_dataset(streaming=True)` or per-file loads that fail on mixed-schema repos (pyarrow CastError).
- No CDN-only fetch path in frontend/training flow: authenticated API calls during data ingestion/training instead of using public CDN URLs.
- Missing reusable Lightning Studio reuse logic: frontend/orchestrator recreates studios instead of reusing running ones, wasting quota.
- No idle-stop guard before `.run()`: Lightning idle timeout kills training; frontend/orchestrator doesn’t check/restart stopped studios.

## 2. Proposed change
Add a small, high-leverage frontend utility module that:
- Persists a `(repo, dateFolder) → file-list` manifest (JSON) after a single authenticated list call.
- Generates CDN-only URLs for each file so downstream training can fetch via CDN (zero API calls).
- Exposes helpers to reuse a running Lightning Studio and restart if idle-stopped.

Scope:  
- Create `/opt/axentx/vanguard/src/utils/hf-cdn-manifest.ts` (or `.js` if project is plain JS).  
- Add `/opt/axentx/vanguard/src/utils/lightning-studio-reuse.ts` with reuse + restart guard.  
- Wire one existing training orchestration entrypoint (e.g., `src/orchestrate-train.js` or similar) to use the manifest and studio reuse (minimal change, high value).

## 3. Implementation

### 3.1 Create `hf-cdn-manifest.ts`
```ts
// /opt/axentx/vanguard/src/utils/hf-cdn-manifest.ts
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { join } from 'path';
import { HfApi } from '@huggingface/hub'; // adjust import if using different HF client

const CACHE_DIR = join(process.cwd(), '.cache', 'hf-manifests');
if (!existsSync(CACHE_DIR)) mkdirSync(CACHE_DIR, { recursive: true });

export interface FileEntry {
  path: string;
  cdnUrl: string; // public CDN URL (no auth)
}

export interface Manifest {
  repo: string;
  dateFolder: string;
  files: FileEntry[];
  generatedAt: string;
}

/**
 * Get or create manifest for (repo, dateFolder).
 * Uses authenticated list_repo_tree ONCE, then persists.
 * Downstream should use cdnUrl for fetches (bypasses HF API rate limits).
 */
export async function getOrCreateManifest(
  repo: string,
  dateFolder: string,
  hfToken?: string
): Promise<Manifest> {
  const slug = repo.replace(/\//g, '--');
  const cachePath = join(CACHE_DIR, `${slug}--${dateFolder}.json`);

  if (existsSync(cachePath)) {
    return JSON.parse(readFileSync(cachePath, 'utf8')) as Manifest;
  }

  const api = new HfApi({ accessToken: hfToken });
  // list_repo_tree(path, recursive=False) per folder to avoid pagination explosion
  const tree = await api.listRepoTree({
    repo,
    path: dateFolder,
    recursive: false,
  });

  const files: FileEntry[] = (tree as any).map((item: any) => ({
    path: item.path,
    cdnUrl: `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(item.path)}`,
  }));

  const manifest: Manifest = {
    repo,
    dateFolder,
    files,
    generatedAt: new Date().toISOString(),
  };

  writeFileSync(cachePath, JSON.stringify(manifest, null, 2), 'utf8');
  return manifest;
}
```

### 3.2 Create `lightning-studio-reuse.ts`
```ts
// /opt/axentx/vanguard/src/utils/lightning-studio-reuse.ts
import { Lightning, Teamspace, Studio, Machine } from '@lightningai/sdk'; // adjust to actual SDK

/**
 * Reuse a running studio or (re)start one.
 * Prevents recreating studios and handles idle-stop deaths.
 */
export async function getOrCreateRunningStudio(
  studioName: string,
  machine: Machine = Machine.L40S,
  teamspace?: string
): Promise<Studio> {
  const ts = teamspace ? Teamspace(teamspace) : Teamspace.current();
  const studios = await ts.studios();

  const existing = studios.find((s) => s.name === studioName);
  if (existing) {
    if (existing.status === 'Running') {
      return existing;
    }
    // Studio exists but stopped (likely killed by idle timeout)
    console.log(`Studio ${studioName} is ${existing.status}. Restarting...`);
    await existing.start({ machine });
    return existing;
  }

  // Create new
  console.log(`Creating studio ${studioName} on ${machine}`);
  return await Studio.create({
    name: studioName,
    machine,
    teamspace: ts,
    createOk: true,
  });
}
```

### 3.3 Wire into orchestration (example)
Locate the frontend-facing or orchestration script that kicks off training (e.g., `src/orchestrate-train.js` or `src/pages/train.tsx`). Replace any per-run `list_repo_tree` and studio creation with the helpers.

Example patch (pseudo, adapt to actual file):
```diff
- const api = new HfApi({ accessToken: HF_TOKEN });
- const tree = await api.listRepoTree({ repo, path: dateFolder, recursive: false });
- const files = tree.map((f) => f.path);
+ import { getOrCreateManifest } from './utils/hf-cdn-manifest';
+ import { getOrCreateRunningStudio } from './utils/lightning-studio-reuse';
+
+ const manifest = await getOrCreateManifest(repo, dateFolder, HF_TOKEN);
+ const files = manifest.files.map((f) => f.cdnUrl); // use CDN URLs in dataloader

- const studio = await Studio.create({ name: 'vanguard-train', machine: Machine.L40S });
+ const studio = await getOrCreateRunningStudio('vanguard-train', Machine.L40S);

- await studio.run({ cmd: 'python train.py ...' });
+ // Guard: if studio stopped, start it first (getOrCreateRunningStudio already does this)
+ await studio.run({ cmd: 'python train.py --data-files ' + JSON.stringify(files) });
```

If the project uses plain Node/JS (no TypeScript), convert `.ts` to `.js` and remove types; keep the same logic.

## 4. Verification
1. Run the orchestration once with a test repo/dateFolder. Confirm:
   - `.cache/hf-manifests/*.json` is created and contains `files[]` with `cdnUrl` entries.
   - No authenticated `listRepoTree` is called on subsequent runs (check logs or monitor HF API usage).
2. Confirm training dataloader uses the CDN URLs (should see `https://huggingface.co/datasets/.../resolve/main/...` in data fetch logs). No Authorization headers should be sent to those URLs.
3. Confirm studio reuse:
   - First run: studio is created.
   - Stop the studio manually (or wait for idle timeout), then re-run orchestration: it should restart the same studio (not create a new one) and training proceeds.
4. Smoke test with a small dataset to ensure no pyarrow CastError (manifest + CDN-only fetch avoids mixed-schema load issues).
