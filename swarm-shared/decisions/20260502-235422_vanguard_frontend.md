# vanguard / frontend

## 1. Diagnosis

- Frontend currently re-lists HF repo files on every training launch, risking 429s and wasting quota.
- No durable manifest: training UI has no persisted file list, so CDN-only fetch strategy can’t be enforced.
- No studio reuse guard: launcher likely creates new Lightning Studio runs instead of reusing Running ones (wastes quota).
- No idle-stop resilience: if a studio stops, subsequent `.run()` calls fail instead of restarting the target.
- Missing CDN bypass path: data loader probably uses HF `datasets` client instead of raw CDN URLs, making training fragile.

## 2. Proposed change

File: `/opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx`  
Scope: add manifest persistence, studio reuse, idle-stop resilience, and CDN-only fetch hints.

## 3. Implementation

```tsx
// /opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx
import { useState, useEffect, useCallback, useRef } from 'react';
import { Teamspace, Studio, Machine } from '@lightningai/sdk';
import axios from 'axios';

const HF_REPO = 'datasets/your-org/vanguard-mirror';
const MANIFEST_PATH = '/tmp/vanguard-manifest.json';

async function listDateFolderOnce(dateFolder: string): Promise<string[]> {
  // Try disk cache first
  try {
    const cached = await axios.get(`file://${MANIFEST_PATH}`).then(r => r.data);
    if (cached?.dateFolder === dateFolder && Array.isArray(cached.files)) {
      return cached.files;
    }
  } catch {
    // ignore
  }

  // Single API call (after rate-limit window)
  const tree = await axios
    .get(`https://huggingface.co/api/datasets/${HF_REPO}/tree`, {
      params: { path: dateFolder, recursive: false },
    })
    .then(r => r.data);

  const files = tree
    .filter((f: any) => f.type === 'file' && f.path.endsWith('.parquet'))
    .map((f: any) => f.path);

  // Persist for training script to embed
  await axios
    .post('file:///tmp/write-manifest', { dateFolder, files })
    .catch(() => {
      // fallback: localStorage for frontend reference (training script should read from disk)
      localStorage.setItem('vanguard-manifest', JSON.stringify({ dateFolder, files }));
    });

  return files;
}

function buildCdnUrls(filePaths: string[]): string[] {
  return filePaths.map(
    p => `https://huggingface.co/datasets/${HF_REPO}/resolve/main/${p}`
  );
}

async function reuseOrCreateStudio(name: string): Promise<Studio> {
  const studios = await Teamspace.studios();
  const running = studios.find(s => s.name === name && s.status === 'Running');
  if (running) return running;

  // If stopped, restart on same machine type to avoid quota churn
  const existing = studios.find(s => s.name === name);
  if (existing) {
    await existing.start({ machine: Machine.L40S });
    return existing;
  }

  return Studio.create({
    name,
    machine: Machine.L40S,
    create_ok: true,
  });
}

export function TrainingLauncher() {
  const [status, setStatus] = useState<'idle' | 'preparing' | 'running'>('idle');
  const studioRef = useRef<Studio | null>(null);

  const launch = useCallback(async () => {
    setStatus('preparing');
    try {
      const dateFolder = new Date().toISOString().slice(0, 10); // e.g. 2026-05-02
      const files = await listDateFolderOnce(dateFolder);
      const cdnUrls = buildCdnUrls(files);

      // Pass CDN-only URLs to training script (zero HF API calls during load)
      const scriptArgs = {
        data_urls: cdnUrls,
        project_to: ['prompt', 'response'],
      };

      studioRef.current = await reuseOrCreateStudio('vanguard-train');
      // Guard against idle-stop death
      if (studioRef.current.status !== 'Running') {
        await studioRef.current.start({ machine: Machine.L40S });
      }

      await studioRef.current.run({
        entrypoint: 'python train.py',
        arguments: [JSON.stringify(scriptArgs)],
      });
      setStatus('running');
    } catch (err) {
      console.error('Launch failed', err);
      setStatus('idle');
    }
  }, []);

  return (
    <div>
      <button onClick={launch} disabled={status === 'preparing'}>
        {status === 'idle' ? 'Launch Training' : status === 'preparing' ? 'Preparing...' : 'Running'}
      </button>
    </div>
  );
}
```

Notes:
- The `/tmp/write-manifest` endpoint is a lightweight backend helper (or use `fs.writeFileSync` if renderer has Node access). If unavailable, fallback to `localStorage` for frontend reference and ensure the Lightning training script reads the persisted JSON from disk.
- Training script (`train.py`) should accept `data_urls` and use CDN-only downloads with `pd.read_parquet`/`pyarrow` directly (no `load_dataset`).

## 4. Verification

1. Open the TrainingLauncher in the vanguard frontend.
2. Click “Launch Training” and confirm:
   - No network 429 errors in browser console during file listing.
   - A file `/tmp/vanguard-manifest.json` (or localStorage entry) is created with `dateFolder` and `files`.
   - Lightning Studio named `vanguard-train` is reused if already Running; otherwise created/started.
   - Studio run log shows training script receiving CDN URLs (no `datasets` HF auth headers).
3. Stop the studio manually, click “Launch Training” again — studio should restart and training should proceed (idle-stop resilience).
4. Re-launch within minutes — verify second run uses cached manifest (no second `tree` API call) and still trains successfully.
