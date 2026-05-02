# vanguard / backend

## Final consolidated solution (strongest + correct + actionable)

**Core diagnosis (merged, de-duplicated)**
- Every training launch re-triggers authenticated `list_repo_tree`/`list_repo_files` → burns HF API quota and risks 429.
- No durable file manifest persisted between runs → training cannot guarantee CDN-only fetches and re-lists each run.
- No sibling-repo sharding for HF commits → ingestion bursts risk 128/hr/repo cap and block progress.
- No idle-guard or studio-reuse check before `.run()` → Lightning idle stop kills training and wastes quota.
- Backend likely recomputes file lists per request instead of caching a date-scoped manifest → high latency, quota-heavy, non-deterministic training inputs.
- (Candidate 2) Risk of pyarrow CastError when `load_dataset(streaming=True)` mixes heterogeneous repos/schemas (avoid by pinning manifest-derived CDN fetches and homogeneous shard selection).

**Single source of truth**
Create one orchestrator module that owns manifest lifecycle, sharding, and studio lifecycle. Prefer file-system persistence (repo + date-scoped) plus in-memory TTL so restarts don’t recompute. Deterministic sharding must be applied consistently for ingestion and training data selection.

**Chosen file paths (concrete)**
- `/opt/axentx/vanguard/src/backend/services/training/trainingOrchestrator.ts` (primary orchestrator)
- `/opt/axentx/vanguard/src/backend/services/training/manifestStore.ts` (manifest persistence + cache)
- `/opt/axentx/vanguard/src/backend/routes/training.ts` (route wiring)

---

## Implementation

### 1) Manifest store (persistence + cache)

```ts
// /opt/axentx/vanguard/src/backend/services/training/manifestStore.ts
import fs from 'fs';
import path from 'path';
import { HFClient } from '../../clients/hfClient'; // your HF client wrapper

const MANIFEST_DIR = process.env.MANIFEST_DIR || '/var/axentx/manifests';
const MANIFEST_TTL_MS = 24 * 60 * 60 * 1000;

if (!fs.existsSync(MANIFEST_DIR)) fs.mkdirSync(MANIFEST_DIR, { recursive: true });

function manifestPath(repo: string, folder: string): string {
  const safeRepo = repo.replace(/[^\w\-]/g, '_');
  const safeFolder = folder.replace(/[^\w\-\.]/g, '_');
  return path.join(MANIFEST_DIR, `manifest-${safeRepo}-${safeFolder}.json`);
}

export interface FileEntry {
  path: string;
  cdnUrl: string;
  size?: number;
}

export interface Manifest {
  repo: string;
  folder: string;
  createdAt: number;
  files: FileEntry[];
}

function isFresh(manifest: Manifest): boolean {
  return Date.now() - manifest.createdAt < MANIFEST_TTL_MS;
}

export async function getOrCreateFileManifest(
  repo: string,
  folder: string,
  hfClient: HFClient
): Promise<Manifest> {
  const p = manifestPath(repo, folder);

  // Try disk cache
  try {
    if (fs.existsSync(p)) {
      const raw = fs.readFileSync(p, 'utf8');
      const parsed: Manifest = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.files) && isFresh(parsed)) {
        return parsed;
      }
    }
  } catch {
    // ignore corrupt manifest and rebuild
  }

  // Build fresh manifest: single non-recursive list per folder
  const items = await hfClient.listRepoTree({ repo, path: folder, recursive: false });
  const files: FileEntry[] = items
    .filter((i: any) => i.type === 'file')
    .map((i: any) => ({
      path: i.path,
      cdnUrl: `https://huggingface.co/datasets/${repo}/resolve/main/${folder}/${encodeURIComponent(i.path)}`,
      size: i.size,
    }));

  const manifest: Manifest = { repo, folder, createdAt: Date.now(), files };

  // Persist
  try {
    fs.writeFileSync(p, JSON.stringify(manifest, null, 2), 'utf8');
  } catch (err) {
    // non-fatal; continue with in-memory manifest
    console.warn('Failed to persist manifest', err);
  }

  return manifest;
}
```

### 2) Training orchestrator (sharding + studio lifecycle + launcher)

```ts
// /opt/axentx/vanguard/src/backend/services/training/trainingOrchestrator.ts
import { getOrCreateFileManifest, Manifest } from './manifestStore';
import { HFClient } from '../clients/hfClient';
import { TrainingLogger } from '../logger';
import { Lightning, Teamspace, Studio, Machine } from '@lightningai/sdk';

const HF_SIBLING_REPOS = [
  'axentx/surrogate-1',
  'axentx/surrogate-1-shard1',
  'axentx/surrogate-1-shard2',
  'axentx/surrogate-1-shard3',
  'axentx/surrogate-1-shard4',
];

function deterministicShardIndex(slug: string): number {
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash) + slug.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash) % HF_SIBLING_REPOS.length;
}

export function pickShardRepo(slug: string): string {
  return HF_SIBLING_REPOS[deterministicShardIndex(slug)];
}

export async function ensureStudioRunning(
  studioName: string,
  machine: Machine = Machine.L40S
): Promise<Studio> {
  const teamspace = await Teamspace.current();
  const running = await teamspace.studios.list();

  const existing = running.find((s) => s.name === studioName && s.status === 'Running');
  if (existing) {
    TrainingLogger.info(`Reusing running studio: ${studioName}`);
    return existing;
  }

  const stopped = running.find((s) => s.name === studioName);
  if (stopped) {
    TrainingLogger.info(`Restarting stopped studio: ${studioName}`);
    await stopped.start({ machine });
    return stopped;
  }

  TrainingLogger.info(`Creating studio: ${studioName}`);
  return await teamspace.studios.create({ name: studioName, machine, createOk: true });
}

export async function runTrainingOnManifest(
  studioName: string,
  repo: string,
  dateFolder: string,
  scriptPath: string,
  hfClient: HFClient
) {
  const manifest = await getOrCreateFileManifest(repo, dateFolder, hfClient);
  const studio = await ensureStudioRunning(studioName);

  // Pass manifest to training via env so script can use CDN-only fetches.
  const result = await studio.run({
    script: scriptPath,
    env: {
      HF_MANIFEST_JSON: JSON.stringify(manifest),
      HF_DATA_REPO: repo,
      HF_DATA_FOLDER: dateFolder,
    },
  });
  return result;
}
```

### 3) Route wiring

```ts
// /opt/axentx/vanguard/src/backend/routes/training.ts
import express from 'express';
import { runTrainingOnManifest, pickShardRepo } from '../services/training/trainingOrchestrator';
import { HFClient } from '../clients/hfClient';

const router = express.Router();
const hfClient = new HFClient(); // configured with token/retry

router.post('/api/training/start', async (req, res) => {
  const { repo, dateFolder, scriptPath, slug } = req.body;
  try {
    const targetRepo = slug ? pickShardRepo(slug) : repo;
    const job = await runTrainingOnManifest('vanguard-training', targetRepo, dateFolder, scriptPath, hfClient);
    res.json({ ok: true, jobId: job.id, repo: targetRepo });
  } catch (err) {
    console.error('Training start failed', err);
    res.status(500).json({ ok: false, error: String(err) });
  }
});

export default router;
```

---

## How training scripts should consume the
