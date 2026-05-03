# airship / frontend

## Highest-Value Incremental Improvement (≤2h)

**What**: Add a CDN-first training slice to Surrogate frontend that:
1. Exposes a small UI + API to trigger “list one date folder from HF” (Mac orchestration) → saves `file-list.json`
2. Embeds that list in the training config so Lightning training uses **only CDN URLs** (zero HF API calls during data load)
3. Reuses running Lightning Studio instead of recreating (saves quota)

**Why**: Eliminates HF API 429s during training, cuts data-load latency, and preserves Lightning quota — all with minimal frontend change.

**Scope**: Frontend-only (React/TypeScript) additions in `/opt/axentx/airship/surrogate` plus one orchestration helper. No infra or backend changes required.

---

## Implementation Plan (≤2h)

| Step | Task | Owner | Time |
|------|------|-------|------|
| 1 | Create `CdnFileLister.ts` — single-call HF tree lister + local JSON writer | FE | 20m |
| 2 | Add `/api/training/file-list` endpoint (or integrate into existing surrogate API surface) to return cached list | FE | 15m |
| 3 | Add small React component `CdnTrainingPanel` with: [List Folder] → shows count + date; [Run Training] → posts config with embedded file list | FE | 30m |
| 4 | Update training launcher (`LightningTrainer.ts`) to accept `fileList: string[]` and generate per-file CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) | FE | 20m |
| 5 | Add Studio reuse guard: list running studios and attach instead of create | FE | 15m |
| 6 | Wire UI into existing training page/route | FE | 10m |
| 7 | Smoke test: list folder → verify JSON → launch training (dry-run) | FE | 10m |

Total: ~2h.

---

## Code Snippets

### 1) CdnFileLister.ts (Mac orchestration helper)

```ts
// surrogate/src/lib/CdnFileLister.ts
import { listRepoTree } from './hfApi'; // thin wrapper around huggingface.co/api

export interface HfFileNode {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

export async function listDateFolder(
  repo: string,
  dateFolder: string
): Promise<string[]> {
  // Example: repo = 'myorg/surrogate-data', dateFolder = 'batches/mirror-merged/2026-04-29'
  const tree = await listRepoTree(repo, dateFolder, { recursive: false });
  const files = tree
    .filter((n) => n.type === 'file')
    .map((n) => `${dateFolder}/${n.path}`);
  return files;
}

export async function saveFileList(
  repo: string,
  dateFolder: string,
  outPath: string
) {
  const files = await listDateFolder(repo, dateFolder);
  const payload = {
    repo,
    folder: dateFolder,
    generatedAt: new Date().toISOString(),
    files,
  };
  await Bun.write(outPath, JSON.stringify(payload, null, 2));
  return payload;
}

// CLI usage (for Mac orchestration):
// bun run src/lib/CdnFileLister.ts --repo myorg/surrogate-data --folder batches/mirror-merged/2026-04-29 --out file-list.json
if (import.meta.main) {
  const args = Bun.argv.slice(2);
  const repo = args.find((a) => a.startsWith('--repo='))?.split('=')[1];
  const folder = args.find((a) => a.startsWith('--folder='))?.split('=')[1];
  const out = args.find((a) => a.startsWith('--out='))?.split('=')[1] || 'file-list.json';

  if (!repo || !folder) {
    console.error('Usage: --repo=owner/repo --folder=path/date --out=file-list.json');
    process.exit(1);
  }

  await saveFileList(repo, folder, out);
  console.log(`Saved ${out} with ${JSON.parse(await Bun.file(out).text()).files.length} files`);
}
```

### 2) Training config + CDN URL builder

```ts
// surrogate/src/lib/LightningTrainer.ts
import type { LightningConfig } from './lightningTypes';

export function buildCdnUrls(fileList: string[], repo: string): string[] {
  // CDN URLs bypass HF API auth/rate limits
  return fileList.map(
    (path) => `https://huggingface.co/datasets/${repo}/resolve/main/${path}`
  );
}

export function createTrainingConfig(
  fileList: string[],
  repo: string,
  options?: Partial<LightningConfig>
): LightningConfig {
  const cdnUrls = buildCdnUrls(fileList, repo);
  return {
    dataset: {
      type: 'cdn_urls',
      urls: cdnUrls,
      repo,
    },
    model: 'Qwen/Qwen2.5-7B-Instruct',
    machine: 'L40S',
    maxSteps: 500,
    ...options,
  };
}
```

### 3) Studio reuse guard

```ts
// surrogate/src/lib/LightningStudio.ts
import { Teamspace, Studio } from 'lightning-ai'; // pseudo import — adapt to actual SDK

export async function getOrCreateRunningStudio(
  name: string,
  machine = 'L40S'
): Promise<Studio> {
  const studios = await Teamspace.studios();
  const running = studios.find((s) => s.name === name && s.status === 'Running');
  if (running) {
    console.log(`Reusing running studio: ${running.id}`);
    return running;
  }

  console.log(`Creating studio: ${name} on ${machine}`);
  return Studio.create({
    name,
    machine,
    // prevent idle timeout kills by setting a reasonable idle timeout if supported
  });
}
```

### 4) API route (example using existing surrogate API pattern)

```ts
// surrogate/src/routes/api/training/file-list.ts
import { listDateFolder } from '$lib/CdnFileLister';
import { json } from '@sveltejs/kit';

export async function POST({ request }) {
  const { repo, folder } = await request.json();
  try {
    const files = await listDateFolder(repo, folder);
    return json({ ok: true, files, count: files.length });
  } catch (err) {
    return json({ ok: false, error: String(err) }, { status: 500 });
  }
}
```

### 5) React panel (minimal)

```tsx
// surrogate/src/components/CdnTrainingPanel.svelte (or .tsx)
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  const dispatch = createEventDispatcher();

  let repo = 'myorg/surrogate-data';
  let folder = 'batches/mirror-merged/2026-04-29';
  let files: string[] = [];
  let loading = false;

  async function listFolder() {
    loading = true;
    const res = await fetch('/api/training/file-list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, folder }),
    });
    const data = await res.json();
    if (data.ok) {
      files = data.files;
    }
    loading = false;
  }

  async function runTraining() {
    // post config with embedded file list
    await fetch('/api/training/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, fileList: files }),
    });
  }
</script>

<div class="panel">
  <h3>CDN-First Training Slice</h3>
  <label>
    Repo
    <input bind:value={repo} />
  </label>
  <label>
    Date folder
    <input bind:value={folder} />
  </
