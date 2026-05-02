# vanguard / frontend

## Final Synthesized Implementation Plan

**Core diagnosis (merged, de-duplicated):**
- Every training launch re-triggers HF repo tree listing (frontend or backend) → 429 risk and quota waste.
- No persisted file manifest in frontend → training cannot use CDN-only fetches; re-listing is unavoidable.
- Lightning Studios are created without checking existing running/stopped ones → quota burn and idle-timeout failures on `.run()`.
- No deterministic HF write-repo selection → commits can concentrate on one repo and hit 128/hr cap.
- No UI affordance for “use persisted manifest” or “reuse studio” → users default to expensive paths.

**Single concrete change (scope + boundaries):**
- Add a frontend training orchestration module that:
  1. Fetches and persists (localStorage) a date-scoped repo file manifest once and reuses it for all training launches.
  2. Lists and reuses running Lightning Studios; restarts stopped studios automatically (L40S default; H200 only in `lightning-lambda-prod`).
  3. Picks a write repo deterministically by hash-slug across 5 siblings to spread HF commits.
  4. Exposes a minimal UI control to launch surrogate-1 training in CDN-only mode.
- Files to create/modify:
  - `src/components/TrainingLauncher.tsx`
  - `src/lib/hf.ts`
  - `src/lib/lightning.ts`
  - `src/lib/training.ts`

---

### `src/lib/hf.ts` — manifest + CDN helpers (deterministic sharding)
```ts
// src/lib/hf.ts
import axios from 'axios';

const HF_API = 'https://huggingface.co/api';
const HF_CDN = 'https://huggingface.co/datasets';

export async function listDateFolder(repo: string, dateFolder: string, token?: string) {
  const res = await axios.get(
    `${HF_API}/datasets/${repo}/tree/${encodeURIComponent(dateFolder)}`,
    {
      params: { recursive: false },
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }
  );
  return res.data; // array<{ path: string; type: string }>
}

export function buildCdnUrl(repo: string, filePath: string) {
  return `${HF_CDN}/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

export function pickWriteRepo(baseRepo: string, slug: string, siblingCount = 5) {
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash) + slug.charCodeAt(i);
    hash |= 0;
  }
  const idx = Math.abs(hash % siblingCount);
  return idx === 0 ? baseRepo : `${baseRepo}-s${idx}`;
}
```

---

### `src/lib/lightning.ts` — studio reuse + restart (correct status handling)
```ts
// src/lib/lightning.ts
import { Lightning } from '@lightningai/sdk'; // adjust to actual import

export async function getOrCreateStudio(
  name: string,
  machine: 'L40S' | 'H200' = 'L40S',
  teamspace?: string
) {
  const client = new Lightning();
  const team = teamspace ? client.teamspace(teamspace) : client.currentTeamspace();
  if (!team) throw new Error('No teamspace available');

  const studios = await team.studios();
  const running = studios.find((s) => s.name === name && s.status === 'Running');
  if (running) return running;

  const stopped = studios.find((s) => s.name === name && s.status === 'Stopped');
  if (stopped) {
    await stopped.start({ machine });
    return stopped;
  }

  return await team.createStudio({
    name,
    machine,
    createOk: true,
  });
}

export async function ensureRunning(studio: any) {
  if (studio.status !== 'Running') {
    await studio.start({ machine: 'L40S' });
  }
  return studio;
}
```

---

### `src/lib/training.ts` — manifest persistence + CDN-only run
```ts
// src/lib/training.ts
import { listDateFolder, buildCdnUrl, pickWriteRepo } from './hf';
import { getOrCreateStudio, ensureRunning } from './lightning';

export interface TrainingOpts {
  repo: string;
  dateFolder: string;
  slug: string;
  hfToken?: string;
  lightningTeam?: string;
}

export async function prepareManifest(opts: TrainingOpts) {
  const key = `manifest/${opts.repo}/${opts.dateFolder}.json`;
  const cached = localStorage.getItem(key);
  if (cached) return JSON.parse(cached);

  const items = await listDateFolder(opts.repo, opts.dateFolder, opts.hfToken);
  const files = items
    .filter((i: any) => i.type === 'file' && i.path.endsWith('.parquet'))
    .map((i: any) => i.path);

  const manifest = { files, generatedAt: Date.now() };
  localStorage.setItem(key, JSON.stringify(manifest));
  return manifest;
}

export async function launchSurrogateTraining(opts: TrainingOpts) {
  const manifest = await prepareManifest(opts);
  const studio = await getOrCreateStudio(`surrogate-${opts.slug}`, 'L40S', opts.lightningTeam);
  await ensureRunning(studio);

  const fileUrls = manifest.files.map((f: string) => buildCdnUrl(opts.repo, f));

  const run = await studio.run({
    command: [
      'python',
      'train.py',
      '--file-list', JSON.stringify(fileUrls),
      '--output-repo', pickWriteRepo(opts.repo, opts.slug),
      '--cdn-only',
    ].join(' '),
  });

  return { run, studio, fileUrls };
}
```

---

### `src/components/TrainingLauncher.tsx` — minimal UI (complete)
```tsx
// src/components/TrainingLauncher.tsx
import React, { useState } from 'react';
import { launchSurrogateTraining } from '../lib/training';

export function TrainingLauncher() {
  const [repo, setRepo] = useState('myorg/surrogate-1');
  const [dateFolder, setDateFolder] = useState('2026-05-02');
  const [slug, setSlug] = useState('run-001');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);

  const handleLaunch = async () => {
    setLoading(true);
    try {
      const res = await launchSurrogateTraining({ repo, dateFolder, slug });
      setResult({ ok: true, studio: res.studio.name, files: res.fileUrls.length });
    } catch (err: any) {
      setResult({ ok: false, error: err.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      <h3>Surrogate-1 Training (CDN-only)</h3>
      <label>
        Repo:
        <input value={repo} onChange={(e) => setRepo(e.target.value)} />
      </label>
      <label>
        Date folder:
        <input value={dateFolder} onChange={(e) => setDateFolder(e.target.value)} />
      </label>
      <label>
        Slug:
        <input value={slug} onChange={(e) => setSlug(e.target.value)} />
      </label>
      <button onClick={handleLaunch} disabled={loading}>
        {loading ? 'Launching...' : 'Launch Surrogate Training'}
      </button>
      {result && (
        <pre style={{ marginTop: 12 }}>
          {result.ok
            ? `OK — studio: ${result.studio}, files: ${result.files}`
            : `Error: ${result.error}`}
        </pre>
      )}
    </div>
  );
}
```
