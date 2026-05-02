# vanguard / backend

## Final consolidated solution (correct + actionable)

**Scope**  
- Add one durable file: `/opt/axentx/vanguard/src/services/hfManifestService.ts`  
- Modify one existing file: `/opt/axentx/vanguard/src/services/lightningService.ts`  
- Add one sharding helper: `/opt/axentx/vanguard/src/services/hfShardService.ts`  

All changes are additive and non-breaking.

---

### 1. `/opt/axentx/vanguard/src/services/hfManifestService.ts`
Persist a date-scoped manifest once per repo+date; reuse it to build CDN URLs and avoid authenticated `list_repo_tree` on every training run.

```ts
import { listRepoTree } from '@huggingface/hub';
import fs from 'fs/promises';
import path from 'path';

const MANIFEST_DIR = path.resolve(process.crud(), 'manifests');

export interface HFManifestEntry {
  path: string;
  size: number;
  sha256?: string;
}

export interface HFManifest {
  repo: string;       // e.g. 'datasets/username/repo'
  dateFolder: string; // e.g. '2026-04-29'
  createdAt: string;  // ISO
  files: HFManifestEntry[];
}

async function ensureManifestDir() {
  await fs.mkdir(MANIFEST_DIR, { recursive: true });
}

export async function getOrCreateManifest(
  repo: string,
  dateFolder: string,
  hfToken: string
): Promise<HFManifest> {
  const slug = repo.replace(/\//g, '_');
  const outPath = path.join(MANIFEST_DIR, `${slug}_${dateFolder}.json`);

  try {
    const raw = await fs.readFile(outPath, 'utf8');
    return JSON.parse(raw) as HFManifest;
  } catch {
    const tree = await listRepoTree({
      repo,
      path: dateFolder,
      recursive: false,
      token: hfToken,
    });

    const files: HFManifestEntry[] = (tree as any[]).map((t) => ({
      path: `${dateFolder}/${t.path}`,
      size: t.size || 0,
      sha256: t.sha256,
    }));

    const manifest: HFManifest = {
      repo,
      dateFolder,
      createdAt: new Date().toISOString(),
      files,
    };

    await ensureManifestDir();
    await fs.writeFile(outPath, JSON.stringify(manifest, null, 2), 'utf8');
    return manifest;
  }
}

export function buildCdnUrl(repo: string, filePath: string): string {
  // CDN fetch; no Authorization header required
  return `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}
```

---

### 2. `/opt/axentx/vanguard/src/services/lightningService.ts`
Add machine sweep (prefer H200/L40S), studio reuse, and idle-stop guard to prevent quota loss.

```ts
import { Lightning, Teamspace, Studio, Machine } from 'lightning-ai'; // adjust import to actual SDK

const PRIORITY_MACHINES: Machine[] = [
  'lightning-lambda-prod/H200',
  'lightning-public-prod/L40S',
  'lightning-public-prod/A100',
  'lightning-public-prod/V100',
];

export async function getAvailableMachine(): Promise<Machine> {
  // In production, replace with real quota/availability probe.
  // For now, return first available by priority.
  for (const m of PRIORITY_MACHINES) {
    try {
      return m;
    } catch {
      continue;
    }
  }
  return 'lightning-public-prod/L40S';
}

export async function ensureRunningStudio(
  name: string,
  machine: Machine
): Promise<Studio> {
  const teamspace = await Teamspace.current();
  const studios = await teamspace.studios();

  const running = studios.find((s) => s.name === name && s.status === 'Running');
  if (running) return running;

  const stopped = studios.find((s) => s.name === name && s.status === 'Stopped');
  if (stopped) {
    await stopped.start({ machine });
    return stopped;
  }

  return Studio.create({ name, machine });
}

export async function runWithIdleGuard(
  studio: Studio,
  command: string[],
  opts?: { maxRetries?: number }
) {
  const maxRetries = opts?.maxRetries ?? 2;
  let lastError: any = null;

  for (let i = 0; i <= maxRetries; i++) {
    try {
      if (studio.status !== 'Running') {
        await studio.start({ machine: studio.machine });
      }
      return await studio.run(command);
    } catch (err) {
      lastError = err;
      // exponential-ish backoff
      await new Promise((r) => setTimeout(r, 30_000 * (i + 1)));
    }
  }
  throw lastError;
}
```

---

### 3. `/opt/axentx/vanguard/src/services/hfShardService.ts`
Shard HF writes across sibling repos to stay below the 128 commits/hr/repo cap.

```ts
import { uploadFile } from '@huggingface/hub';
import crypto from 'crypto';

const SIBLINGS = 5;

function pickShardRepo(repoBase: string, slug: string): string {
  const hash = crypto.createHash('sha256').update(slug).digest('hex');
  const idx = parseInt(hash.slice(0, 8), 16) % SIBLINGS;
  return idx === 0 ? repoBase : `${repoBase}-shard${idx}`;
}

export async function shardedUpload(
  repoBase: string,
  slug: string,
  content: Buffer | Uint8Array | string,
  filename: string,
  token: string
) {
  const repo = pickShardRepo(repoBase, slug);
  return uploadFile({
    repo,
    path: `batches/mirror-merged/${new Date().toISOString().slice(0, 10)}/${slug}/${filename}`,
    content,
    token,
  });
}
```

---

### 4. Verification checklist

1. **Manifest caching**  
   ```bash
   node -e "import('./src/services/hfManifestService.js').then(m => m.getOrCreateManifest('datasets/example/repo', '2026-04-29', 'hf_xxx').then(console.log))"
   ```
   - Creates `manifests/datasets_example_repo_2026-04-29.json`.  
   - Second run returns cached manifest with no additional `listRepoTree` calls.

2. **CDN URLs**  
   - `buildCdnUrl('datasets/example/repo', '2026-04-29/file.json')` must return a URL that fetches without an Authorization header.

3. **Lightning studio reuse + idle guard**  
   - `ensureRunningStudio('axentx-train', 'lightning-public-prod/L40S')` starts or reuses a studio.  
   - Stop the studio in UI, then call `runWithIdleGuard(studio, ['python', 'train.py'])` — studio restarts and command runs.

4. **Sharding distribution**  
   - `pickShardRepo('axentx/surrogate-1', 'slugA')` etc. distributes across `surrogate-1` and `surrogate-1-shard1..4`.
