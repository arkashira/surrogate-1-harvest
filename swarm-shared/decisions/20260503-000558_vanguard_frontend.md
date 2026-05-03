# vanguard / frontend

## 1. Diagnosis
- No persisted, CDN-only manifest for `(repo, dateFolder)` → every training launch re-enumerates via authenticated HF API, burning quota and risking 429.
- Frontend cannot pre-flight or cache available files; users pick invalid/missing paths and trigger late, expensive failures.
- Training script still relies on `load_dataset(streaming=True)` over heterogeneous repos → `pyarrow.CastError` on mixed schemas.
- No reuse check for running Lightning Studio → quota waste (new studio per launch) and 80hr/mo unnecessary burn.
- Idle-stop kills training; frontend has no status polling or auto-restart for stopped studios.

## 2. Proposed change
Add a frontend “Training Orchestrator” module that:
- Generates and caches a CDN-only file manifest for a selected `(repo, dateFolder)` (single authenticated call, then CDN-only thereafter).
- Persists manifest to `localStorage` + optional server copy (`/manifests/{repo}/{dateFolder}.json`) so training jobs are reproducible without re-enumeration.
- Checks for an existing running Lightning Studio and reuses it; if stopped, restarts with `L40S` target before launch.
- Exposes a minimal UI (button + status panel) in the existing training page (assumed at `src/pages/Training.vue` or similar) to trigger and monitor orchestration.

Scope:
- New file: `src/composables/useTrainingOrchestrator.js`
- Update: `src/pages/Training.vue` (or create if missing) to wire UI.
- Optional: `src/utils/hf-cdn.js` for CDN-only fetches and manifest I/O.

## 3. Implementation

### src/composables/useTrainingOrchestrator.js
```js
import { Lightning } from '@lightningai/sdk';
import { Teamspace } from '@lightningai/sdk';
import { Machine } from '@lightningai/sdk';

const HF_API = 'https://huggingface.co/api';
const HF_CDN = 'https://huggingface.co/datasets';

export function useTrainingOrchestrator() {
  const manifestCache = new Map();

  async function listRepoFolder(repo, dateFolder, token) {
    // Single authenticated call; cache aggressively.
    const key = `${repo}/${dateFolder}`;
    if (manifestCache.has(key)) return manifestCache.get(key);

    const res = await fetch(`${HF_API}/repos/datasets/${repo}/tree/${encodeURIComponent(dateFolder)}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new Error(`HF tree failed: ${res.status}`);
    const tree = await res.json();
    // Only direct files in this folder (non-recursive).
    const files = (tree.tree || [])
      .filter((t) => t.type === 'blob' && t.path.startsWith(dateFolder + '/'))
      .map((t) => t.path);
    manifestCache.set(key, files);
    return files;
  }

  async function buildCDNManifest(repo, dateFolder, token) {
    const files = await listRepoFolder(repo, dateFolder, token);
    const manifest = files.map((path) => ({
      path,
      cdn: `${HF_CDN}/${repo}/resolve/main/${encodeURIComponent(path)}`,
    }));
    // Persist for reproducibility (training script will embed this list).
    try {
      localStorage.setItem(`vanguard-manifest-${repo}-${dateFolder}`, JSON.stringify(manifest));
    } catch (e) {
      // ignore storage limits
    }
    return manifest;
  }

  async function getOrCreateRunningStudio(name, teamspace = 'default') {
    const studios = await Teamspace.studios(teamspace);
    const running = studios.find((s) => s.name === name && s.status === 'Running');
    if (running) return running;

    // If exists but stopped, restart.
    const stopped = studios.find((s) => s.name === name && s.status === 'Stopped');
    if (stopped) {
      await stopped.start({ machine: Machine.L40S });
      return stopped;
    }

    // Create new (only if none exist).
    const studio = await Lightning.Studio.create({
      name,
      teamspace,
      machine: Machine.L40S,
      create_ok: true,
    });
    return studio;
  }

  async function launchTrainingJob({ repo, dateFolder, hfToken, lightningTeamspace, jobName }) {
    // 1) Build CDN-only manifest (single API call).
    const manifest = await buildCDNManifest(repo, dateFolder, hfToken);
    if (!manifest.length) throw new Error('No files found for folder');

    // 2) Reuse or create studio.
    const studio = await getOrCreateRunningStudio(jobName, lightningTeamspace);

    // 3) Run training script with manifest baked in (avoids HF API during data load).
    // Training script must accept manifest via env or CLI.
    const run = await studio.run({
      command: `bash run_surrogate_train.sh ${repo} ${dateFolder}`,
      env: {
        HF_REPO: repo,
        HF_DATEFOLDER: dateFolder,
        HF_MANIFEST_JSON: JSON.stringify(manifest),
        // Avoid streaming heterogeneous datasets; use manifest + hf_hub_download per file.
        USE_CDN_ONLY: '1',
      },
    });
    return { studio, run };
  }

  async function pollStudioStatus(studio) {
    // Lightweight status check to detect idle-stop and restart if needed.
    const updated = await studio.refresh();
    if (updated.status === 'Stopped') {
      await updated.start({ machine: Machine.L40S });
      return 'restarted';
    }
    return updated.status;
  }

  return {
    buildCDNManifest,
    launchTrainingJob,
    pollStudioStatus,
  };
}
```

### src/pages/Training.vue (minimal wiring)
```vue
<template>
  <div class="training-orchestrator">
    <h2>Surrogate-1 Training</h2>

    <div class="form-row">
      <label>Repo (datasets/...)</label>
      <input v-model="repo" placeholder="org/surrogate-1" />
    </div>
    <div class="form-row">
      <label>Date folder</label>
      <input v-model="dateFolder" placeholder="2026-04-29" />
    </div>
    <div class="form-row">
      <label>HF Token (optional for private)</label>
      <input type="password" v-model="hfToken" />
    </div>

    <button @click="doLaunch" :disabled="launching">
      {{ launching ? 'Launching...' : 'Launch Training' }}
    </button>

    <div v-if="status" class="status">
      <strong>Status:</strong> {{ status }}
    </div>
    <div v-if="manifest" class="manifest">
      <strong>Manifest ({{ manifest.length }} files)</strong>
      <pre>{{ manifest.slice(0, 5).map(m => m.path).join('\n') }}</pre>
    </div>
  </div>
</template>

<script>
import { ref } from 'vue';
import { useTrainingOrchestrator } from '../composables/useTrainingOrchestrator';

export default {
  setup() {
    const { launchTrainingJob, buildCDNManifest } = useTrainingOrchestrator();

    const repo = ref('');
    const dateFolder = ref('');
    const hfToken = ref('');
    const launching = ref(false);
    const status = ref('');
    const manifest = ref(null);

    async function doLaunch() {
      if (!repo.value || !dateFolder.value) {
        status.value = 'repo and date folder required';
        return;
      }
      launching.value = true;
      status.value = 'building manifest...';
      try {
        manifest.value = await buildCDNManifest(repo.value, dateFolder.value, hfToken.value || undefined);
        status.value = 'manifest ready — launching studio...';
        const { studio } = await launchTrainingJob({
          repo: repo.value,
          dateFolder: dateFolder.value,
          hfToken: hfToken.value || undefined,
          lightningTeamspace: 'default',
          jobName: `vanguard-${repo.value.replace('/', '-')}-${dateFolder.value}`,
        });
        status.value = `studio
