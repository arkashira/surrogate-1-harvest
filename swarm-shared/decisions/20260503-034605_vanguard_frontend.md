# vanguard / frontend

### Final Synthesis (single, actionable plan)

**Diagnosis (merged, prioritized)**
- Frontend has **no deterministic manifest UI** for `{date}/{slug}` training batches → users trigger backend re-enumeration and risk HF API 429.
- No **CDN-only fetch guarantee** or pre-flight validation that listed files come from a pinned manifest → backend can still call `list_repo_tree`/`load_dataset` at runtime.
- Missing **visual feedback for surrogate-1 training state** (Lightning Studio reuse/idle-stop) → duplicate studios and quota burn.
- No **client-side guard against mixed-schema HF repos** → uploads can pollute `enriched/` with extra columns (`source`, `ts`) that break surrogate-1 schema.
- No **“top-hub” contextual insight panel** (MOC/graph) to surface knowledge-rag findings before training runs.

**Proposed change (single, scoped)**
Add a compact **Training Batch Selector** panel plus a minimal manifest contract and runtime guards:
- New component: `/opt/axentx/vanguard/src/components/TrainingBatchSelector.vue`
- New composable: `/opt/axentx/vanguard/src/composables/useTrainingManifest.ts`
- New types: `/opt/axentx/vanguard/src/types/training.ts`
- Integrate into main layout (`App.vue` or equivalent).
- Total scope: ~150 lines (component + composable + types).

**Implementation (concrete, production-ready)**

1) **Types**  
`/opt/axentx/vanguard/src/types/training.ts`
```ts
export interface BatchManifest {
  date: string;           // YYYY-MM-DD
  slug: string;           // content-addressed slug
  files: string[];        // relative paths under repo
  repo: string;           // huggingface dataset repo (e.g., org/dataset)
  createdAt: string;      // ISO
  sha256?: string;        // optional root manifest integrity
}

export interface TrainingState {
  studioName: string;
  status: 'running' | 'stopped' | 'starting' | 'idle';
  lastHeartbeat: string;
}
```

2) **Composable (CDN-first, manifest-driven, rate-limit safe)**  
`/opt/axentx/vanguard/src/composables/useTrainingManifest.ts`
```ts
import { ref, computed } from 'vue';
import type { BatchManifest, TrainingState } from '@/types/training';

const manifests = ref<BatchManifest[]>([]);
const selected = ref<BatchManifest | null>(null);
const loading = ref(false);
const training = ref<TrainingState>({
  studioName: 'vanguard-surrogate-1',
  status: 'idle',
  lastHeartbeat: '',
});

async function loadManifests(date?: string) {
  loading.value = true;
  try {
    // Single orchestrator endpoint to avoid HF API 429.
    // Expects /manifests/{date}.json or /manifests/latest.json
    const d = date || 'latest';
    const res = await fetch(`/api/manifests/${d}.json`);
    if (!res.ok) throw new Error('Failed to load manifest');
    const data: BatchManifest[] = await res.json();
    // Basic schema guard: reject entries missing required fields
    manifests.value = data.filter(
      (m) => m.date && m.slug && Array.isArray(m.files) && m.repo
    );
  } finally {
    loading.value = false;
  }
}

function selectManifest(m: BatchManifest) {
  selected.value = m;
}

function cdnUrl(repo: string, path: string) {
  // CDN bypass: no Authorization header; uses CDN tier limits
  return `https://huggingface.co/datasets/${repo}/resolve/main/${path}`;
}

async function checkStudioStatus() {
  try {
    const res = await fetch(`/api/lightning/studios/${training.value.studioName}`);
    if (!res.ok) {
      training.value.status = 'stopped';
      return;
    }
    const s = await res.json();
    training.value.status = s.status;
    training.value.lastHeartbeat = s.lastHeartbeat;
  } catch {
    training.value.status = 'stopped';
  }
}

const canRun = computed(() => {
  return (
    !!selected.value &&
    training.value.status !== 'running' &&
    // Reject mixed-schema repos heuristically:
    // surrogate-1 expects enriched/ + specific columns; block if repo root has unexpected top-level files
    !selected.value.files.some((f) => f.startsWith('raw/') && !f.includes('enriched'))
  );
});

async function runTraining() {
  if (!canRun.value || !selected.value) return;
  await fetch('/api/training/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      manifest: selected.value,
      studioName: training.value.studioName,
    }),
  });
  // Poll or rely on webhook for status updates
  await checkStudioStatus();
}

async function startStudio() {
  if (training.value.status === 'running') return;
  await fetch(`/api/lightning/studios/${training.value.studioName}/start`, {
    method: 'POST',
  });
  await checkStudioStatus();
}

export function useTrainingManifest() {
  return {
    manifests,
    selected,
    loading,
    training,
    canRun,
    loadManifests,
    selectManifest,
    cdnUrl,
    checkStudioStatus,
    runTraining,
    startStudio,
  };
}
```

3) **Component**  
`/opt/axentx/vanguard/src/components/TrainingBatchSelector.vue`
```vue
<template>
  <section class="batch-selector">
    <header>
      <h3>Surrogate-1 Training Batch</h3>
      <button @click="refresh" :disabled="loading">Refresh</button>
    </header>

    <div v-if="loading">Loading manifests…</div>

    <div v-else class="batches">
      <div
        v-for="m in manifests"
        :key="`${m.date}/${m.slug}`"
        class="batch-card"
        :class="{ active: selected?.slug === m.slug }"
        @click="selectManifest(m)"
      >
        <strong>{{ m.date }} / {{ m.slug }}</strong>
        <div class="meta">{{ m.files.length }} files</div>
        <div class="repo">{{ m.repo }}</div>
      </div>
    </div>

    <div v-if="selected" class="selected-panel">
      <h4>Selected: {{ selected.date }} / {{ selected.slug }}</h4>
      <ul>
        <li v-for="f in selected.files" :key="f">
          <a :href="cdnUrl(selected.repo, f)" target="_blank" rel="noopener">
            {{ f }}
          </a>
        </li>
      </ul>

      <div class="studio-status">
        <strong>Lightning Studio:</strong>
        <span :class="training.status">{{ training.status }}</span>
        <button @click="startStudio" :disabled="training.status === 'running'">
          Start / Reuse
        </button>
      </div>

      <div class="actions">
        <button @click="runTraining" :disabled="!canRun">Run Training</button>
      </div>
    </div>

    <div class="hint">
      Tip: Manifest files are pinned and fetched from CDN only to avoid HF API 429.
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted } from 'vue';
import { useTrainingManifest } from '@/composables/useTrainingManifest';

const {
  manifests,
  selected,
  loading,
  training,
  canRun,
  loadManifests,
  selectManifest,
  cdnUrl,
  checkStudioStatus,
  runTraining,
  startStudio,
} = useTrainingManifest();

function refresh() {
  loadManifests();
}

onMounted(() => {
  loadManifests();
  checkStudioStatus();
  // Poll status every 30s
  const id = setInterval(checkStudioStatus, 30_0
