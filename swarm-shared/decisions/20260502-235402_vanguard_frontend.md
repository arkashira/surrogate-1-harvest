# vanguard / frontend

## 1. Diagnosis

- Frontend has no durable HF manifest strategy: training UI likely re-lists repo files on every run, risking 429s and wasting quota.
- No CDN-only fetch path: data loader probably uses HF API/`datasets` client instead of raw CDN URLs, making training fragile under rate limits.
- Missing sibling-repo sharding in upload path: large mirror/ingest writes could hit the 128/hr commit cap on a single repo.
- Lightning Studio lifecycle not reused: frontend may trigger `Studio(create_ok=True)` repeatedly instead of reusing running studios, burning quota.
- No deterministic fallback when Lightning idle-stops: training jobs may silently die instead of auto-restarting on L40S/H200.

## 2. Proposed change

Add a frontend service that:
- Generates and persists a CDN-only file manifest (single API call per date folder).
- Produces signed CDN URLs for training data (zero API calls during load).
- Shards uploads across 5 sibling repos by hash slug.
- Reuses running Lightning studios and auto-restarts idle ones.

File scope:
- New: `/opt/axentx/vanguard/src/services/hfManifestService.ts`
- Modify: `/opt/axentx/vanguard/src/services/lightningService.ts`

## 3. Implementation

### New file: `hfManifestService.ts`

```ts
// /opt/axentx/vanguard/src/services/hfManifestService.ts
import { listRepoTree } from './hfApiService';

const CDN_BASE = 'https://huggingface.co/datasets';
const SIBLINGS = 5;

function pickSiblingRepo(slug: string): string {
  // deterministic sharding: hash slug → sibling repo suffix
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash) + slug.charCodeAt(i);
    hash |= 0;
  }
  const idx = Math.abs(hash) % SIBLINGS;
  // expected sibling naming: org/dataset-mirror-0 .. dataset-mirror-4
  const base = 'dataset-mirror';
  return idx === 0 ? base : `${base}-${idx}`;
}

export interface FileEntry {
  path: string;
  cdnUrl: string;
  size: number;
}

export interface Manifest {
  repo: string;
  folder: string;
  generatedAt: string;
  files: FileEntry[];
}

export async function buildCdnManifest(
  repo: string,
  folder: string = ''
): Promise<Manifest> {
  // single API call: list top-level tree for folder (non-recursive if you paginate by depth)
  const tree = await listRepoTree(repo, folder, false);
  const files: FileEntry[] = [];

  for (const node of tree) {
    if (node.type !== 'file') continue;
    // CDN URL bypasses API auth/rate limits
    const cdnUrl = `${CDN_BASE}/${repo}/resolve/main/${node.path}`;
    files.push({
      path: node.path,
      cdnUrl,
      size: node.size || 0,
    });
  }

  return {
    repo,
    folder,
    generatedAt: new Date().toISOString(),
    files,
  };
}

export function pickUploadRepo(slug: string): string {
  return pickSiblingRepo(slug);
}

export function buildCdnTrainingUrls(manifest: Manifest): string[] {
  // return only parquet files (or filter by pattern)
  return manifest.files
    .filter((f) => f.path.endsWith('.parquet'))
    .map((f) => f.cdnUrl);
}
```

### Modify: `lightningService.ts`

```diff
// /opt/axentx/vanguard/src/services/lightningService.ts
+ import { Teamspace, Studio, Machine } from 'lightningai';

  const teamspace = new Teamspace('vanguard-team');

+ async function findRunningStudio(name: string) {
+   const studios = await teamspace.studios();
+   return studios.find((s) => s.name === name && s.status === 'Running');
+ }

- export async function runTraining(jobName: string, scriptPath: string) {
-   const studio = new Studio({ name: jobName, create_ok: true });
-   await studio.run(scriptPath);
- }

+ export async function runTraining(jobName: string, scriptPath: string) {
+   let studio = await findRunningStudio(jobName);
+   if (!studio) {
+     studio = new Studio({ name: jobName, create_ok: true });
+     // prefer L40S; fallback to free-tier machines
+     await studio.start(Machine.L40S);
+   }
+
+   // If studio exists but stopped, restart it
+   if (studio.status !== 'Running') {
+     await studio.start(Machine.L40S);
+   }
+
+   await studio.run(scriptPath);
+ }

+ export async function ensureIdleRestart(jobName: string, scriptPath: string) {
+   const studio = await findRunningStudio(jobName);
+   if (!studio || studio.status !== 'Running') {
+     // restart on L40S (or H200 in paid account if available)
+     const machine = Machine.L40S;
+     const s = studio || new Studio({ name: jobName, create_ok: true });
+     await s.start(machine);
+     await s.run(scriptPath);
+   }
+ }
```

## 4. Verification

1. Build manifest (once per folder/date):
   ```ts
   import { buildCdnManifest, buildCdnTrainingUrls } from './services/hfManifestService';
   const manifest = await buildCdnManifest('org/dataset-mirror', '2026-05-02');
   const urls = buildCdnTrainingUrls(manifest);
   console.log(urls); // should be raw CDN URLs (no /api/)
   ```
   Confirm:
   - No Authorization header required to fetch listed URLs.
   - Single call to `listRepoTree` produced the list.

2. Sharding:
   ```ts
   import { pickUploadRepo } from './services/hfManifestService';
   console.log(pickUploadRepo('batches/mirror-merged/2026-05-02/slug123.parquet'));
   ```
   Confirm deterministic repo among `dataset-mirror` .. `dataset-mirror-4`.

3. Lightning reuse + restart:
   - Start a studio manually, then call `runTraining('test-job', 'train.py')`. Confirm it reuses the running studio (no new studio created).
   - Stop the studio, call `ensureIdleRestart('test-job', 'train.py')`. Confirm it restarts and runs.

4. Rate-limit safety:
   - Disable HF token or simulate 429 on API calls; confirm CDN URLs still fetch.
