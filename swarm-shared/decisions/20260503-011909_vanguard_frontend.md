# vanguard / frontend

## Final Synthesis (Corrected + Actionable)

**Core diagnosis (merged, de-duplicated):**
- Authenticated HF API calls (`list_repo_tree`, dataset metadata) run on page load/training start, burning 1000/5min quota and causing 429s.
- File fetches use authenticated `/api/` paths instead of public CDN URLs, preventing rate-limit bypass and adding latency.
- No persisted `(repo, dateFolder)` file manifest; every session re-enumerates files.
- Lightning Studio sessions are not reused; new sessions are created and idle-stop kills training, wasting quota and confusing users.
- Frontend provides no clear UX feedback for HF rate limits or Lightning idle-stop kills; failures appear opaque.
- Heavy compute or model loads may run locally on the Mac instead of being delegated to Lightning/Kaggle/Cerebrum.

**Chosen strategy (resolve contradictions in favor of correctness + actionability):**
- Use CDN-only URLs for file fetches; never send Authorization headers to HuggingFace for dataset file downloads.
- Generate and commit a static JSON manifest at build/orchestration time; load it once and cache it.
- Reuse a running Lightning Studio session; never create a new one if a running one exists.
- Provide explicit, user-facing status for quota/rate-limit and idle-stop events.
- Keep all heavy training and model loading inside Lightning Studio (not on the Mac).

---

## Implementation (single, integrated plan)

### 1) Project setup

```bash
cd /opt/axentx/vanguard
```

### 2) Add/Update HF helper (CDN + manifest)

`src/api/hf.ts`:

```ts
// src/api/hf.ts
const CDN_ROOT = 'https://huggingface.co/datasets';

export type FileManifest = {
  repo: string;
  dateFolder: string;
  files: string[];
};

let cachedManifest: FileManifest | null = null;

export async function loadManifest(repo: string, dateFolder: string): Promise<FileManifest> {
  // Manifest is pre-generated and served from /public/manifests/
  const res = await fetch(`/manifests/${repo}/${dateFolder}.json`);
  if (!res.ok) {
    throw new Error(`Manifest not found: ${repo}/${dateFolder}`);
  }
  cachedManifest = await res.json();
  return cachedManifest;
}

// CDN-only file fetch (no Authorization header)
export function getCdnFileUrl(repo: string, filePath: string): string {
  return `${CDN_ROOT}/${repo}/resolve/main/${filePath}`;
}

export async function fetchParquetAsArrayBuffer(repo: string, filePath: string): Promise<ArrayBuffer> {
  const url = getCdnFileUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`CDN fetch failed: ${url} ${res.status}`);
  return res.arrayBuffer();
}
```

### 3) Lightning Studio reuse helper

`src/lib/studio.ts`:

```ts
// src/lib/studio.ts
import { Lightning } from '@lightningai/sdk'; // adjust import to actual SDK

export async function getRunningStudio(name: string) {
  const studios = await Lightning.Teamspace.studios();
  return studios.find((s) => s.name === name && s.status === 'Running');
}

export async function ensureStudioRunning(
  name: string,
  machine: any
) {
  let studio = await getRunningStudio(name);
  if (studio) return studio;

  studio = await Lightning.Studio.create({ name, create_ok: true });
  await studio.start({ machine });
  return studio;
}
```

### 4) Update training page (manifest + CDN + studio reuse + UX)

`src/pages/Train.tsx`:

```tsx
// src/pages/Train.tsx
import { useEffect, useState } from 'react';
import { loadManifest, fetchParquetAsArrayBuffer } from '../api/hf';
import { ensureStudioRunning } from '../lib/studio';

export default function TrainPage() {
  const [status, setStatus] = useState('idle');
  const repo = 'myorg/surrogate-1';
  const dateFolder = '2026-04-29';

  async function startTraining() {
    setStatus('loading-manifest');
    try {
      const manifest = await loadManifest(repo, dateFolder);
      setStatus('manifest-loaded');

      if (manifest.files.length > 0) {
        setStatus('prefetching');
        await fetchParquetAsArrayBuffer(repo, manifest.files[0]);
        setStatus('prefetch-ok');
      }

      setStatus('ensuring-studio');
      const studio = await ensureStudioRunning('vanguard-trainer', { name: 'L40S' });
      setStatus('studio-running');

      const run = await studio.run({
        command: `python train.py --manifest /manifests/${repo}/${dateFolder}.json`,
      });
      setStatus(`started-run: ${run.id}`);
    } catch (err: any) {
      // Distinguish likely rate-limit vs studio/idle failures for UX
      const msg = err.message || String(err);
      if (msg.includes('429') || msg.toLowerCase().includes('rate limit')) {
        setStatus('error-rate-limit');
      } else if (msg.includes('idle') || msg.includes('killed')) {
        setStatus('error-idle-stop');
      } else {
        setStatus('error-unknown');
      }
      console.error(err);
    }
  }

  return (
    <div>
      <h1>Surrogate-1 Training</h1>
      <p>Status: {status}</p>
      <button
        onClick={startTraining}
        disabled={status.includes('loading') || status.includes('running') || status.includes('error')}
      >
        Start Training
      </button>
      {status === 'error-rate-limit' && (
        <p style={{ color: 'red' }}>Rate limit hit. Try again after HF API window clears.</p>
      )}
      {status === 'error-idle-stop' && (
        <p style={{ color: 'red' }}>Studio was idle-stopped. Restarting...</p>
      )}
    </div>
  );
}
```

### 5) Manifest generator (run on Mac orchestration/CI)

`scripts/generate-manifest.js`:

```js
// scripts/generate-manifest.js
const { HfApi } = require('@huggingface/hub');
const fs = require('fs');
const path = require('path');

async function main() {
  const api = new HfApi();
  const repo = 'myorg/surrogate-1';
  const dateFolder = '2026-04-29';
  const tree = await api.listRepoTree(repo, path.join('batches', 'mirror-merged', dateFolder), { recursive: false });

  const files = tree
    .filter((t) => t.type === 'file' && t.path.endsWith('.parquet'))
    .map((t) => t.path);

  const manifest = { repo, dateFolder, files };
  const outDir = path.join(__dirname, '..', 'public', 'manifests');
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, `${dateFolder}.json`), JSON.stringify(manifest, null, 2));
  console.log(`Wrote manifest with ${files.length} files`);
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

Add to `package.json`:

```json
"scripts": {
  "gen:manifest": "node scripts/generate-manifest.js"
}
```

### 6) Verification checklist

1. Generate manifest (after HF API window is clear):
   ```bash
   cd /opt/axentx/vanguard
   npm run gen:manifest
   ```
   Confirm `public/manifests/2026-04-29.json` exists and lists parquet files.

2. Start dev server and open Train page:
   - Click “Start Training”.
   - Observe status sequence: `loading-manifest` → `manifest-loaded` → `prefetching` → `prefetch-ok` → `ensuring-studio` → `studio-running` → `started-run: ...`.
