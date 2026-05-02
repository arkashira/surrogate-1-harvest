# vanguard / frontend

## 1. Diagnosis

- Frontend currently re-lists HF repo files on every training launch, risking 429s and wasting quota.
- No durable HF manifest strategy: no persisted file list for a given date folder, so CDN-only fetch path can’t be enforced.
- Data loader likely uses HF `datasets` client (API-backed) instead of raw CDN URLs, making training fragile under rate limits.
- No studio reuse logic: launcher probably recreates studios instead of reusing running ones, burning Lightning quota.
- No idle-stop guard: if a studio stops, `.run()` will fail instead of restarting on an available machine.

## 2. Proposed change

File: `/opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx`  
Scope: add a durable manifest + CDN-only fetch strategy, studio reuse, and idle-stop resilience.

## 3. Implementation

```tsx
// /opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx
import { useState, useEffect, useCallback, useRef } from 'react';
import { Teamspace, Studio, Machine } from '@lightningai/sdk';
import axios from 'axios';

const HF_REPO = 'datasets/your-org/surrogate-1';
const MANIFEST_DIR = '/tmp/vanguard-manifests';

async function listDateFolder(dateFolder: string): Promise<string[]> {
  // One API call per date folder (after rate-limit window clears)
  const res = await axios.get(
    `https://huggingface.co/api/datasets/${HF_REPO}/tree/${dateFolder}`,
    { params: { recursive: false } }
  );
  // Only return file paths (CDN URLs later)
  return (res.data || []).map((f: any) => `${dateFolder}/${f.path}`);
}

async function getOrCreateManifest(dateFolder: string): Promise<string[]> {
  const fs = window.require?.('fs') || (await import('fs')).promises;
  const path = window.require?.('path') || (await import('path')).default;
  const manifestPath = path.join(MANIFEST_DIR, `${dateFolder.replace(/\//g, '_')}.json`);

  try {
    await fs.access(manifestPath);
    const raw = await fs.readFile(manifestPath, 'utf8');
    return JSON.parse(raw);
  } catch {
    const files = await listDateFolder(dateFolder);
    await fs.mkdir(MANIFEST_DIR, { recursive: true });
    await fs.writeFile(manifestPath, JSON.stringify(files), 'utf8');
    return files;
  }
}

function buildCdnUrls(filePaths: string[]): string[] {
  return filePaths.map(
    (p) => `https://huggingface.co/datasets/${HF_REPO}/resolve/main/${p}`
  );
}

async function ensureRunningStudio(name: string): Promise<Studio> {
  const teamspace = await Teamspace.current();
  const studios = await teamspace.studios();

  const existing = studios.find((s) => s.name === name && s.status === 'Running');
  if (existing) return existing;

  // Reuse stopped studio if present
  const stopped = studios.find((s) => s.name === name && s.status === 'Stopped');
  if (stopped) {
    await stopped.start({ machine: Machine.L40S });
    return stopped;
  }

  return Studio.create({
    name,
    machine: Machine.L40S,
    createOk: true,
  });
}

export default function TrainingLauncher() {
  const [status, setStatus] = useState<'idle' | 'preparing' | 'running' | 'error'>('idle');
  const [log, setLog] = useState<string[]>([]);
  const studioRef = useRef<Studio | null>(null);

  const runTraining = useCallback(async (dateFolder: string) => {
    setStatus('preparing');
    try {
      const files = await getOrCreateManifest(dateFolder);
      const urls = buildCdnUrls(files);

      // Persist minimal file list for training script (CDN-only)
      const fs = window.require?.('fs') || (await import('fs')).promises;
      const manifestOut = `/tmp/vanguard-manifests/train_files_${Date.now()}.json`;
      await fs.writeFile(manifestOut, JSON.stringify(urls), 'utf8');

      const studio = await ensureRunningStudio('vanguard-train');
      studioRef.current = studio;

      setStatus('running');
      setLog((l) => [...l, `Using ${urls.length} files (CDN-only)`]);

      // Lightweight launcher script that reads manifest and trains via CDN
      const runId = await studio.run({
        entrypoint: 'bash',
        args: [
          '/workspace/scripts/train_cdn.sh',
          manifestOut,
          HF_REPO,
        ],
        // Avoids datasets client; train_cdn.sh uses wget/curl against CDN URLs
      });

      setLog((l) => [...l, `Run submitted: ${runId}`]);
    } catch (err: any) {
      setStatus('error');
      setLog((l) => [...l, `Error: ${err.message}`]);
    }
  }, []);

  // Monitor studio status and restart if stopped unexpectedly
  useEffect(() => {
    if (!studioRef.current) return;
    const id = setInterval(async () => {
      try {
        const s = await Teamspace.current().then((t) => t.studios());
        const studio = s.find((x) => x.id === studioRef.current?.id);
        if (studio && studio.status === 'Stopped') {
          setLog((l) => [...l, 'Studio stopped — restarting...']);
          await studio.start({ machine: Machine.L40S });
        }
      } catch {
        // ignore transient errors
      }
    }, 30000);
    return () => clearInterval(id);
  }, []);

  return (
    <div style={{ padding: 16 }}>
      <h3>Vanguard Training Launcher</h3>
      <div>
        <button onClick={() => runTraining('batches/mirror-merged/2026-04-29')} disabled={status === 'preparing' || status === 'running'}>
          Train (2026-04-29)
        </button>
      </div>
      <pre style={{ background: '#f6f6f6', padding: 8, marginTop: 12, maxHeight: 300, overflow: 'auto' }}>
        {log.map((x, i) => `${i + 1}. ${x}`).join('\n')}
      </pre>
      <div>Status: {status}</div>
    </div>
  );
}
```

Create companion script (Lightning Studio side):

```bash
# /workspace/scripts/train_cdn.sh
#!/usr/bin/env bash
set -euo pipefail

MANIFEST="$1"
REPO="$2"

echo "Reading CDN file list from $MANIFEST"
mapfile -t URLS < <(jq -r '.[]' "$MANIFEST")

# Example: stream from CDN without HF API/auth
# Replace with your actual training command.
python -c "
import json, urllib.request, sys
urls = json.load(open('$MANIFEST'))
print(f'Will fetch {len(urls)} files via CDN')
# Implement your dataset streaming / preprocessing here using raw HTTP(S).
# Avoid datasets.load_dataset() to bypass API rate limits.
"
```

Make executable:

```bash
chmod +x /workspace/scripts/train_cdn.sh
```

## 4. Verification

1. Open the TrainingLauncher in the frontend.
2. Click “Train (2026-04-29)”.
3. Confirm:
   - A manifest file is created under `/tmp/vanguard-manifests/` (check logs).
   - Log shows “Using N files (CDN-only)” with correct count.
   - Studio is reused or started (check Lightning console for “vanguard-train” running).
   - `train_cdn.sh` is invoked and prints the CDN file count (check Studio logs).
4. Re-run the same date: verify no new HF `list_repo_tree` API call is made (manifest is reused).
5. Stop the studio manually in Lightning console, wait 30s, and verify the auto-restart log appears and the studio transitions back to Running.
