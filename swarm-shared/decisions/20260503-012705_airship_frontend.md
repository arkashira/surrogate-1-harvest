# airship / frontend

## Final Synthesized Implementation Plan  
*Combines strongest, most actionable parts from both proposals; resolves contradictions in favor of correctness + concrete actionability.*

---

## 1) Core Objective (Highest-Value Incremental Improvement)
Implement an **HF CDN-bypass dataset loader** + **Lightning Studio reuse** in the training UI to:
- Eliminate HF API 429s and `pyarrow.CastError`s during dataset loading  
- Reduce Lightning quota burn by reusing running studios  
- Ship as a focused frontend enhancement in **<2h**

---

## 2) Architecture Decisions (Resolved Contradictions)
| Decision | Chosen approach | Rationale |
|----------|----------------|-----------|
| **Loader location** | Frontend service (`datasetService.ts`) + thin optional proxy (`/api/dataset/cdn-list`) | Avoids CORS; keeps orchestration simple; matches Candidate 2’s service pattern while preserving Candidate 1’s orchestrator idea via optional backend call. |
| **File listing** | Single non-recursive `listRepoTreeFolder(repo, dateFolder)` per date folder; cache `file-list.json` in memory/UI | Minimizes API calls; prevents repeated listing; aligns with Candidate 1’s “once per date folder” and Candidate 2’s non-recursive approach. |
| **CDN URL format** | `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth header) | Universally accessible; bypasses 429; both candidates agree. |
| **Studio reuse** | `getOrCreateStudio(name, machine)` + `ensureRunning(studio)` guard | Combines Candidate 1’s reuse-by-name and Candidate 2’s explicit ensure-running + L40S fallback. |
| **Training payload** | Embed `dataset_urls` (CDN) + projection to `{prompt, response}` only | Prevents mixed-schema errors; matches Candidate 1’s acceptance criteria. |
| **UI toggle** | “Dataset Source” toggle: “HuggingFace CDN (no-auth)” vs “HF API” | Candidate 2’s toggle is clearer for users; keeps fallback path. |

---

## 3) Implementation Plan (1h 45m total)

| Phase | Time | Tasks |
|-------|------|-------|
| **1. Audit & Locate** | 10m | Find training UI components (`TrainingPage.tsx` or `TrainingForm.tsx`). Identify dataset loader and Lightning launcher hooks. Confirm repo/folder conventions. |
| **2. CDN-bypass Service** | 30m | Create `src/services/datasetService.ts` with `listRepoTreeFolder`, `buildCdnFileUrls`, and optional `downloadViaCdnBatch`. Add thin proxy endpoints if needed for CORS/auth. |
| **3. Lightning Studio Service** | 25m | Create `src/services/lightningService.ts` with `getOrCreateStudio`, `ensureRunning`, and `submitTrainingJob` that injects CDN file list into training args/env. |
| **4. UI Integration** | 25m | Wire training form: add Dataset Source toggle, repo/date inputs, file list preview, and “Prepare training” → “Start Training” flow. Show reused studio name + status badge. |
| **5. Server-side Data Module** | 10m | Add `CDNParquetDataset` (or equivalent) that consumes CDN URLs and projects to `{prompt, response}`. |
| **6. Test & Polish** | 5m | Verify CDN URLs load without auth, studio reuse prevents duplicates, idle-stop guard restarts studio, and schema projection is correct. |

---

## 4) Code Snippets (Final, Consolidated)

### 1) CDN-bypass dataset service (`src/services/datasetService.ts`)
```ts
import { list_repo_tree } from './hf-api'; // thin wrapper around huggingface_hub

export interface FileEntry {
  path: string;
  repo: string;
}

export async function listRepoTreeFolder(repo: string, dateFolder: string) {
  // Single API call per date folder (non-recursive)
  const tree = await list_repo_tree(repo, dateFolder, { recursive: false });
  return tree.entries.filter((e) => e.type === 'file' && e.path.endsWith('.parquet'));
}

export function buildCdnFileUrls(repo: string, entries: Array<{ path: string }>) {
  return entries.map((e) => ({
    path: e.path,
    url: `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(e.path)}`,
  }));
}

// Optional: streaming download with abort
export async function downloadViaCdnBatch(urls: string[], onProgress?: (loaded: number, total: number) => void) {
  const results: Array<{ url: string; data: ArrayBuffer }> = [];
  for (const url of urls) {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${url}`);
    const data = await res.arrayBuffer();
    results.push({ url, data });
    onProgress?.(results.length, urls.length);
  }
  return results;
}
```

### 2) Lightning Studio service (`src/services/lightningService.ts`)
```ts
import { Lightning, Teamspace, Machine, Studio } from 'lightning-ai';

export async function getOrCreateStudio(studioName: string, machine = Machine.L40S) {
  const teamspace = new Teamspace();
  const running = teamspace.studios.find(
    (s) => s.name === studioName && s.status === 'Running'
  );
  if (running) return running;

  // create only if not running
  return Studio({ create_ok: true }).start({ machine, name: studioName });
}

export async function ensureRunning(studio: Studio, machine = Machine.L40S) {
  if (studio.status !== 'Running') {
    await studio.start({ machine });
  }
  return studio;
}

export async function submitTrainingJob(
  studioName: string,
  trainScript: string,
  datasetUrls: string[]
) {
  const studio = await getOrCreateStudio(studioName);
  await ensureRunning(studio);

  const target = {
    script: trainScript,
    project: 'surrogate',
    args: {
      dataset_urls: datasetUrls,
      // projection handled in training script
    },
  };
  return studio.run(target);
}
```

### 3) Training UI integration (`TrainingPage.tsx`)
```tsx
import { useState, useEffect } from 'react';
import { listRepoTreeFolder, buildCdnFileUrls } from '@/services/datasetService';
import { submitTrainingJob } from '@/services/lightningService';

export default function TrainingPage() {
  const [repo, setRepo] = useState('datasets/axentx/surrogate-mirror');
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [source, setSource] = useState<'cdn' | 'hf'>('cdn');
  const [files, setFiles] = useState<Array<{ path: string; url: string }>>([]);
  const [studioName] = useState('surrogate-trainer');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (source === 'cdn') {
      listRepoTreeFolder(repo, dateFolder).then((entries) => {
        setFiles(buildCdnFileUrls(repo, entries));
      });
    } else {
      setFiles([]);
    }
  }, [repo, dateFolder, source]);

  const handleStart = async () => {
    setLoading(true);
    try {
      const urls = files.map((f) => f.url);
      await submitTrainingJob(studioName, 'train.py', urls);
    } catch (err) {
      console.error('Training launch failed', err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); handleStart(); }}>
      <label>
        Dataset Source:
        <select value={source} onChange={(e) => setSource(e.target.value as any)}>
          <option value="cdn">HuggingFace CDN (no-auth)</option>
          <option value="hf">HF API</option>
        </select>
      </label>

      <label>
        Repo:
        <input value={repo} onChange={(e) =>
