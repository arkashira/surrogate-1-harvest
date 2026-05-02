# vanguard / frontend

## Final synthesized implementation (best of both proposals)

**Core principles**
- One HF tree API call per `(repo, dateFolder)`; persist to `localStorage` (not sessionStorage) so restarts and new tabs reuse it.
- All file URLs must be CDN-only (`resolve/main/...`) so training scripts and preview/streaming never require authenticated HF calls.
- Reuse running studios; auto-restart stopped studios; surface status in UI.
- Deterministic, cacheable manifest drives training parameters (no ad-hoc/hardcoded values).
- Minimal, copy-paste-ready files with concrete actions and verification steps.

---

### 1) Manifest service (single source of truth)

File: `/opt/axentx/vanguard/src/frontend/src/lib/training/hfFileStore.ts`
```ts
// Centralized HF manifest + CDN URL builder.
// One tree call per (repo, dateFolder) persisted to localStorage.

const API_ROOT = 'https://huggingface.co';
const CDN_ROOT = 'https://huggingface.co/datasets';
const TTL_MS = 1000 * 60 * 10; // 10 min cache; adjust as needed

export interface HFManifest {
  repo: string;           // e.g. "axentx/surrogate-1"
  dateFolder: string;     // e.g. "2026-04-29"
  files: string[];        // paths relative to repo root, e.g. "2026-04-29/file.parquet"
  generatedAt: number;
}

function storageKey(repo: string, dateFolder: string) {
  return `hfManifest::${repo}::${dateFolder}`;
}

export async function getOrCreateManifest(
  repo: string,
  dateFolder: string,
  opts?: { forceRefresh?: boolean; token?: string }
): Promise<HFManifest> {
  const key = storageKey(repo, dateFolder);
  const cachedRaw = typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;

  if (!opts?.forceRefresh && cachedRaw) {
    const cached = JSON.parse(cachedRaw) as HFManifest;
    if (Date.now() - cached.generatedAt < TTL_MS) return cached;
  }

  const headers: Record<string, string> = {};
  if (opts?.token) headers.Authorization = `Bearer ${opts.token}`;

  // Non-recursive list of the dateFolder only
  const res = await fetch(`${API_ROOT}/api/datasets/${repo}/tree/${dateFolder}`, { headers });
  if (!res.ok) throw new Error(`HF tree list failed: ${res.status} ${res.statusText}`);
  const nodes: Array<{ path: string; type: 'file' | 'directory' }> = await res.json();

  // Flat file list within this dateFolder (training script expects deterministic ordering)
  const files = nodes
    .filter((n) => n.type === 'file')
    .map((n) => `${dateFolder}/${n.path}`)
    .sort();

  const manifest: HFManifest = { repo, dateFolder, files, generatedAt: Date.now() };
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(key, JSON.stringify(manifest));
  }
  return manifest;
}

export function cdnUrl(repo: string, filePath: string): string {
  // filePath must include dateFolder prefix
  return `${CDN_ROOT}/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

export function clearManifest(repo: string, dateFolder: string) {
  if (typeof localStorage !== 'undefined') {
    localStorage.removeItem(storageKey(repo, dateFolder));
  }
}
```

---

### 2) Studio pool (reuse + auto-restart)

File: `/opt/axentx/vanguard/src/frontend/src/lib/training/studioPool.ts`
```ts
// Reuse running studios; restart stopped ones; avoid unnecessary creates.
// Uses Lightning SDK (assumed available).

export interface RunningStudio {
  id: string;
  name: string;
  status: 'running' | 'stopped' | 'starting' | 'stopping';
  machine?: string;
}

async function listStudios(): Promise<RunningStudio[]> {
  // @ts-ignore - Lightning SDK global
  const studios = await Lightning.Teamspace.studios?.() ?? [];
  return studios.map((s: any) => ({
    id: s.id,
    name: s.name,
    status: s.status,
    machine: s.machine
  }));
}

export async function findRunningStudio(name: string): Promise<RunningStudio | null> {
  const studios = await listStudios();
  const s = studios.find((x) => x.name === name && x.status === 'running');
  return s || null;
}

export async function ensureStudioRunning(
  name: string,
  targetMachine = 'L40S'
): Promise<RunningStudio> {
  const studios = await listStudios();
  const existing = studios.find((s) => s.name === name);

  if (existing) {
    if (existing.status === 'running') return existing;
    if (existing.status === 'stopped') {
      // @ts-ignore
      await Lightning.Studio(existing.id).start?.({ machine: targetMachine });
      return { id: existing.id, name, status: 'starting', machine: targetMachine };
    }
    // starting/stopping: return current state; caller can poll or wait
    return existing;
  }

  // Create new studio if none exists
  // @ts-ignore
  const studio = await Lightning.Studio.create?.({ name, machine: targetMachine, create_ok: true });
  return { id: studio.id, name: studio.name, status: studio.status, machine: studio.machine };
}
```

---

### 3) Launcher (manifest + CDN + studio reuse)

File: `/opt/axentx/vanguard/src/frontend/src/lib/training/trainingLauncher.ts`
```ts
import { getOrCreateManifest, cdnUrl } from './hfFileStore';
import { ensureStudioRunning } from './studioPool';

export interface TrainingSpec {
  repo: string;
  dateFolder: string;
  scriptPath: string; // repo-relative, e.g. "scripts/train.py"
  studioName: string;
  machine?: string;
  hfToken?: string; // optional: for manifest fetch only
}

export async function launchTraining(spec: TrainingSpec) {
  // 1) Deterministic manifest (cached)
  const manifest = await getOrCreateManifest(spec.repo, spec.dateFolder, {
    forceRefresh: false,
    token: spec.hfToken
  });

  // 2) CDN-only URLs (no Authorization headers required by CDN)
  const fileUrls = manifest.files.map((f) => cdnUrl(spec.repo, f));

  // 3) Reuse or restart studio
  const studio = await ensureStudioRunning(spec.studioName, spec.machine || 'L40S');

  // 4) Run training script with manifest injected as env
  // Training script should consume MANIFEST_FILES (CDN URLs) and avoid HF API calls.
  // @ts-ignore
  const run = await Lightning.Studio(studio.id).run?.({
    command: [
      'bash',
      '-c',
      `
      export MANIFEST_FILES='${JSON.stringify(fileUrls)}'
      export HF_DATASET_REPO='${spec.repo}'
      cd /workspace && python ${spec.scriptPath}
      `
    ],
    cwd: '/workspace',
    timeoutSeconds: 300
  });

  return { studio, run, manifest, fileUrls };
}
```

---

### 4) UI hook (Svelte) — minimal and testable

File: `/opt/axentx/vanguard/src/frontend/src/routes/training/+page.svelte`
```svelte
<script lang="ts">
  import { launchTraining } from '$lib/training/trainingLauncher';

  let loading = false;
  let lastRun: Awaited<ReturnType<typeof launchTraining>> | null = null;
  let error: string | null = null;

  async function start() {
    loading = true;
    error = null;
    try {
      lastRun = await launchTraining({
        repo: 'axentx/surrogate-1',
        dateFolder: '2026-04-29',
        scriptPath: 'scripts
