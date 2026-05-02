# vanguard / backend

Below is the **single, final synthesized implementation**.  
It merges the strongest, non-contradictory parts of both proposals and prioritizes **correctness + concrete actionability**.

Key synthesis decisions:
- **Manifest persistence** (both agree): list once, store on disk, reuse → eliminates repeated HF tree calls and 429 risk.
- **CDN-only fetches** (both agree): manifest embeds CDN URLs; training uses only those.
- **Deterministic sibling-repo sharding for writes** (C1) + **explicit repo per folder in manifest** (C2): merged so manifest records logical repo, while write path deterministically picks a sibling for commit load-spreading.
- **Lightning idle-guard + reuse** (both agree): check running status, restart only if stopped, prefer running studios, and sweep clouds/sizes in priority order to find H200/L40S.
- **Mac/CLI orchestration guardrails**: CLI must delegate to Lightning-backed runner; local training forbidden.
- **File locations**: unified under `trainingService.ts` (orchestration + launch) and `manifestService.ts` (persistence) for clarity and testability.

---

## 1. Types

File: `/opt/axentx/vanguard/src/backend/types/training.ts`

```ts
export interface TrainingFile {
  path: string;
  cdnUrl: string;
  sizeBytes?: number;
}

export interface TrainingManifest {
  dateFolder: string;     // e.g. batches/mirror-merged/2026-04-29
  createdAt: string;
  logicalRepo: string;    // canonical repo for reads (e.g. datasets/axentx/surrogate-1)
  files: TrainingFile[];
  cdnOnly: true;
}

export interface LightningStudioSpec {
  name: string;
  cloud?: 'lightning-lambda-prod' | 'public';
  size?: 'L40S' | 'H200' | 'A100' | 'H100';
}
```

---

## 2. Manifest service (persistence + CDN URLs)

File: `/opt/axentx/vanguard/src/backend/services/manifestService.ts`

```ts
import fs from 'fs/promises';
import path from 'path';
import { createHash } from 'crypto';
import { TrainingManifest, TrainingFile } from '../types/training';

const MANIFEST_DIR = path.join(process.cwd(), 'manifests');
const HF_CDN_BASE = 'https://huggingface.co/datasets';

function buildCdnUrl(repo: string, filePath: string): string {
  // Ensure proper encoding for spaces/special chars in path
  return `${HF_CDN_BASE}/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

export class ManifestService {
  private baseDir: string;

  constructor(baseDir?: string) {
    this.baseDir = baseDir ?? MANIFEST_DIR;
  }

  private manifestPath(dateFolder: string): string {
    return path.join(this.baseDir, `${dateFolder.replace(/\//g, '_')}.json`);
  }

  async ensureDir() {
    await fs.mkdir(this.baseDir, { recursive: true });
  }

  async load(dateFolder: string): Promise<TrainingManifest | null> {
    await this.ensureDir();
    const p = this.manifestPath(dateFolder);
    try {
      const raw = await fs.readFile(p, 'utf8');
      return JSON.parse(raw) as TrainingManifest;
    } catch {
      return null;
    }
  }

  async save(manifest: TrainingManifest): Promise<void> {
    await this.ensureDir();
    const p = this.manifestPath(manifest.dateFolder);
    await fs.writeFile(p, JSON.stringify(manifest, null, 2), 'utf8');
  }

  buildManifest(dateFolder: string, logicalRepo: string, filePaths: string[]): TrainingManifest {
    const files: TrainingFile[] = filePaths.map((fp) => ({
      path: fp,
      cdnUrl: buildCdnUrl(logicalRepo, fp),
      sizeBytes: 0,
    }));

    return {
      dateFolder,
      createdAt: new Date().toISOString(),
      logicalRepo,
      files,
      cdnOnly: true,
    };
  }

  pickWriteRepo(slug: string, siblings: string[]): string {
    const hash = createHash('sha256').update(slug).digest('hex');
    const idx = parseInt(hash.slice(0, 8), 16) % siblings.length;
    return siblings[idx];
  }
}
```

---

## 3. Training orchestration + Lightning idle-guard + cloud/size sweep

File: `/opt/axentx/vanguard/src/backend/services/trainingService.ts`

```ts
import { ManifestService } from './manifestService';
import { TrainingManifest } from '../types/training';
import { Lightning } from '../clients/lightning-client';
import { huggingface } from '../clients/hf-client';
import { backoffRetry } from '../utils/retry';

const SIBLING_REPOS = [
  'axentx/surrogate-1',
  'axentx/surrogate-1-sib1',
  'axentx/surrogate-1-sib2',
  'axentx/surrogate-1-sib3',
  'axentx/surrogate-1-sib4',
];

export class TrainingService {
  private manifestService = new ManifestService();

  /**
   * Prepare (or reuse) a manifest for a date folder.
   * Performs at most one HF listRepoTree call per folder (with retry/backoff).
   */
  async prepareTrainingManifest(dateFolder: string): Promise<TrainingManifest> {
    const existing = await this.manifestService.load(dateFolder);
    if (existing) return existing;

    // Single non-recursive list for the folder (paginated safe)
    const filePaths = await backoffRetry(() =>
      huggingface.listRepoTree({
        repo: 'datasets/axentx/surrogate-1',
        path: dateFolder,
        recursive: false,
      }).then((entries) => entries.map((e) => `${dateFolder}/${e.path}`))
    );

    const manifest = this.manifestService.buildManifest(
      dateFolder,
      'datasets/axentx/surrogate-1',
      filePaths
    );
    await this.manifestService.save(manifest);
    return manifest;
  }

  pickWriteRepo(slug: string): string {
    return this.manifestService.pickWriteRepo(slug, SIBLING_REPOS);
  }

  /**
   * Ensure a Lightning Studio is running.
   * If stopped -> restart with L40S and wait until Running.
   * If missing -> throw (do not auto-create; prefer explicit provisioning).
   */
  async ensureStudioRunning(studioName: string): Promise<void> {
    const studios = await Lightning.teamspace.listStudios();
    const studio = studios.find((s) => s.name === studioName);

    if (!studio) {
      throw new Error(`Studio ${studioName} not found. Provision it first.`);
    }

    if (studio.status === 'Running') {
      return;
    }

    if (studio.status === 'Stopped') {
      console.log(`Studio ${studioName} is stopped. Restarting with L40S...`);
      await studio.start({ machine: 'L40S' });
    } else {
      // Pending/Starting/Error: wait briefly and re-check
      console.log(`Studio ${studioName} is ${studio.status}. Waiting to become Running...`);
    }

    // Poll until Running (with timeout)
    const maxAttempts = 30;
    for (let i = 0; i < maxAttempts; i++) {
      const s = await Lightning.teamspace.getStudio(studioName);
      if (s.status === 'Running') return;
      if (s.status === 'Error') {
        throw new Error(`Studio ${studioName} entered Error state`);
      }
      await new Promise((r) => setTimeout(r, 10000));
    }

    throw new Error(`Studio ${studioName} did not reach Running state within timeout`);
  }

  /**
   * Sweep clouds × sizes in priority order to find an available machine.
   * Returns the first available { cloud, size } or null.
   */
  async findAvailableMachine(
    preferred
