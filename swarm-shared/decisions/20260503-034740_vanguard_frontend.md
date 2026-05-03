# vanguard / frontend

## 1. Diagnosis

- No frontend manifest for CDN-only dataset fetches: training UI likely triggers runtime `list_repo_tree`/`load_dataset` calls that hit HF API rate limits and break reproducibility.
- Missing content-addressed file list: frontend cannot pre-compute or cache which parquet files belong to a given `{date}/{slug}` without an embedded manifest.
- No studio-reuse awareness in UI: training job launcher probably creates new Lightning Studio sessions instead of reusing running ones, wasting quota.
- Idle-stop handling absent: frontend does not check studio status before `.run()`; idle timeouts kill training and leave users guessing.
- No CDN-bypass download path: file URLs are probably constructed via HF API endpoints instead of `https://huggingface.co/datasets/{repo}/resolve/main/...`, exposing requests to auth rate limits.

## 2. Proposed change

Add a small, high-leverage frontend module that:
- Exposes a deterministic `fileList` JSON (content-addressed by `{date}/{slug}`) for CDN-only fetches.
- Reuses running Lightning Studio instances and handles idle-stop restart.
- Uses CDN URLs exclusively (no Authorization header) for dataset files.

Scope:  
- Create `/opt/axentx/vanguard/src/lib/datasetManifest.ts`  
- Create `/opt/axentx/vanguard/src/lib/studioClient.ts`  
- Update `/opt/axentx/vanguard/src/routes/train/+page.ts` (or equivalent) to import and use them.

## 3. Implementation

### `src/lib/datasetManifest.ts`
```ts
// Deterministic, content-addressed manifest for CDN-only fetches.
// Generated once on the Mac (or CI) after rate-limit window clears.
// Embedded into the frontend build so Lightning training does zero HF API calls.

export interface FileManifest {
  repo: string;           // e.g. "datasets/axentx/surrogate-1"
  date: string;           // YYYY-MM-DD
  slug: string;           // content-addressed slug
  files: string[];        // relative paths under resolve/main/
  generatedAt: string;    // ISO timestamp
}

export function buildFileManifest(
  repo: string,
  date: string,
  slug: string,
  filePaths: string[]
): FileManifest {
  return {
    repo,
    date,
    slug,
    files: filePaths.sort(),
    generatedAt: new Date().toISOString(),
  };
}

// CDN-only URL constructor (no Authorization header).
export function cdnUrl(manifest: FileManifest, filePath: string): string {
  return `https://huggingface.co/datasets/${manifest.repo}/resolve/main/${filePath}`;
}
```

### `src/lib/studioClient.ts`
```ts
import { Lightning, Teamspace, Studio, Machine } from "@lightning-ai/sdk";

// Reuse running studios to save quota (80hr/mo).
export async function getOrCreateStudio(
  name: string,
  machine: Machine = Machine.L40S
): Promise<Studio> {
  const teamspace = await Teamspace.current();
  const running = await teamspace.studios.list({ status: "Running" });

  const existing = running.find((s) => s.name === name);
  if (existing) {
    return existing;
  }

  return await teamspace.studios.create({
    name,
    machine,
    createOk: true,
  });
}

// Handle Lightning idle-stop: restart studio if stopped before run.
export async function ensureRunning(studio: Studio, machine: Machine = Machine.L40S): Promise<void> {
  const refreshed = await studio.fetch();
  if (refreshed.status !== "Running") {
    await studio.start({ machine });
    // Poll briefly for running state (simple backoff in real impl).
    await new Promise((r) => setTimeout(r, 15000));
  }
}
```

### `src/routes/train/+page.ts` (or equivalent route handler)
```ts
import { buildFileManifest, cdnUrl } from "$lib/datasetManifest";
import { getOrCreateStudio, ensureRunning } from "$lib/studioClient";

// Example: frontend launcher for surrogate-1 training job.
export async function runTrainingJob(date: string, slug: string) {
  // This file list should be generated once and imported/bundled.
  // For demo, we inline a minimal example; in prod, import a generated JSON.
  const manifest = buildFileManifest(
    "datasets/axentx/surrogate-1",
    date,
    slug,
    [`batches/mirror-merged/${date}/${slug}.parquet`]
  );

  // Use CDN URLs exclusively in training script args/env.
  const fileUrls = manifest.files.map((f) => cdnUrl(manifest, f));

  const studio = await getOrCreateStudio(`vanguard-train-${slug}`);
  await ensureRunning(studio);

  // Pass CDN-only URLs to training via env or CLI args.
  await studio.run({
    command: `python train.py --file-urls '${JSON.stringify(fileUrls)}'`,
    // Avoid HF API calls during data load: Lightning will fetch via CDN.
    env: {
      HF_DATASETS_OFFLINE: "1",
      HF_HUB_OFFLINE: "1",
    },
  });
}
```

## 4. Verification

1. **Manifest correctness**:  
   - Generate a manifest for a known `{date}/{slug}` and confirm `files` lists expected parquet paths.  
   - Confirm `cdnUrl()` produces URLs of form `https://huggingface.co/datasets/.../resolve/main/...` (no `api/`, no Authorization header required).

2. **Studio reuse**:  
   - Start a studio manually via UI or SDK.  
   - Call `getOrCreateStudio("vanguard-train-test")` twice; second call must return the same running instance (no new studio created).

3. **Idle-stop handling**:  
   - Let the studio idle-stop.  
   - Call `ensureRunning()` and verify it restarts the studio and `status` becomes `Running`.

4. **CDN-only fetch during training**:  
   - Run the training job and monitor network requests from the Lightning node.  
   - Confirm no requests to `huggingface.co/api/*` or `huggingface.co/datasets/*` that require Authorization.  
   - Confirm parquet files are fetched successfully from CDN URLs and training proceeds without 429s.
