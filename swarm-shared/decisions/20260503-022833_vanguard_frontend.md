# vanguard / frontend

## 1. Diagnosis
- No frontend manifest cache: every preview/training launch triggers authenticated HF API calls (`list_repo_tree`, `load_dataset`) → burns quota and risks 429s.
- No CDN-bypass: data loads route through authenticated `/api/` endpoints instead of public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) → higher rate-limit pressure and slower fetches.
- No pre-listed file manifest: training scripts re-enumerate repo contents on every run instead of using a static file list → redundant API calls and fragility during rate-limit windows.
- No graceful 429 fallback: frontend/training flows fail immediately on 429 instead of exponential backoff + CDN fallback → brittle user experience.
- Idle-stop fragility: Lightning Studio training dies on idle stop and frontend doesn’t auto-restart → lost progress and manual intervention.

## 2. Proposed change
- **File scope**: `/opt/axentx/vanguard/src/frontend/utils/hfClient.ts` (create if missing) and `/opt/axentx/vanguard/src/frontend/pages/training/[runId]/page.tsx` (or equivalent launcher).
- **Goal**: add a lightweight HF client that:
  1. Pre-lists files once (Mac orchestration) and emits `file-manifest.json`.
  2. Uses CDN-only URLs for dataset file fetches during training.
  3. Caches manifest in `localStorage`/`IndexedDB` and falls back to CDN if API 429.
  4. Exposes `getDatasetFileUrls(repo, path)` and `fetchWithCdnFallback`.

## 3. Implementation

### Create `/opt/axentx/vanguard/src/frontend/utils/hfClient.ts`
```ts
// HF CDN-bypass client for Vanguard frontend
// - Pre-listed manifest preferred; CDN URLs used for all data fetches
// - Graceful 429 fallback + exponential backoff

const HF_CDN_ROOT = 'https://huggingface.co/datasets';
const HF_API_ROOT = 'https://huggingface.co/api';
const MANIFEST_KEY = 'hf-manifest-v1';

export interface FileEntry {
  path: string;
  size?: number;
  type: 'file' | 'directory';
}

export interface Manifest {
  repo: string;
  root: string;
  files: FileEntry[];
  generatedAt: string;
}

function getCdnUrl(repo: string, path: string): string {
  return `${HF_CDN_ROOT}/${repo}/resolve/main/${path}`;
}

async function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

export async function fetchWithCdnFallback(
  repo: string,
  path: string,
  opts: RequestInit & { maxRetries?: number } = {}
): Promise<Response> {
  const { maxRetries = 3, ...init } = opts;
  const url = getCdnUrl(repo, path);

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const res = await fetch(url, { ...init, credentials: 'omit' });
      // CDN 429 unlikely but handle consistently
      if (res.status === 429) {
        const retryAfter = Number(res.headers.get('retry-after')) || 2 ** attempt;
        await sleep(retryAfter * 1000);
        continue;
      }
      return res;
    } catch (err) {
      if (attempt === maxRetries) throw err;
      await sleep(2 ** attempt * 1000);
    }
  }
  throw new Error('Exhausted retries for CDN fetch');
}

export async function getCachedManifest(repo: string, root: string): Promise<Manifest | null> {
  try {
    const raw = localStorage.getItem(`${MANIFEST_KEY}:${repo}:${root}`);
    if (!raw) return null;
    const m: Manifest = JSON.parse(raw);
    // consider stale after 24h
    if (Date.now() - new Date(m.generatedAt).getTime() > 86400_000) return null;
    return m;
  } catch {
    return null;
  }
}

export function setCachedManifest(manifest: Manifest) {
  try {
    localStorage.setItem(`${MANIFEST_KEY}:${manifest.repo}:${manifest.root}`, JSON.stringify(manifest));
  } catch {
    // ignore storage limits
  }
}

// Prefer pre-listed manifest; if absent and API allowed, try to list one level (non-recursive)
// NOTE: Mac orchestration should generate and embed manifest; this is a fallback.
export async function listRepoTreeNonRecursive(
  repo: string,
  root: string = ''
): Promise<FileEntry[]> {
  const cached = await getCachedManifest(repo, root);
  if (cached) return cached.files;

  try {
    const res = await fetch(`${HF_API_ROOT}/datasets/${repo}/tree?path=${encodeURIComponent(root)}&recursive=false`, {
      headers: { Accept: 'application/json' },
      credentials: 'omit'
    });
    if (res.status === 429) {
      // Do not hammer API — return empty to force CDN-only mode
      console.warn('HF API 429 — falling back to CDN-only mode');
      return [];
    }
    if (!res.ok) throw new Error(`HF tree API error: ${res.status}`);
    const items: Array<{ path: string; type: 'file' | 'directory'; size?: number }> = await res.json();
    const files = items.map((i) => ({ path: i.path, type: i.type, size: i.size }));
    const manifest: Manifest = { repo, root, files, generatedAt: new Date().toISOString() };
    setCachedManifest(manifest);
    return files;
  } catch (err) {
    console.warn('Failed to list repo tree:', err);
    return [];
  }
}

// Build CDN URLs for known parquet files under a date folder (common Surrogate-1 pattern)
export function getDatasetFileUrls(repo: string, dateFolder: string): string[] {
  // Intended usage: manifest should be provided by orchestration.
  // This helper builds likely CDN paths for a date folder.
  return [
    `batches/mirror-merged/${dateFolder}/*.parquet`,
    `enriched/${dateFolder}/*.parquet`
  ].map((p) => `${HF_CDN_ROOT}/${repo}/resolve/main/${p.replace('*', '{file}')}`);
}
```

### Update launcher / training page to embed manifest and use CDN-only data loading

Example snippet for training launcher (adjust path to your actual page/component):

```tsx
// /opt/axentx/vanguard/src/frontend/pages/training/[runId]/page.tsx  (or equivalent)
import { useEffect, useState } from 'react';
import { getCachedManifest, fetchWithCdnFallback, getDatasetFileUrls } from '@/utils/hfClient';

export default function TrainingRunPage({ repo, dateFolder }: { repo: string; dateFolder: string }) {
  const [manifest, setManifest] = useState<any>(null);
  const [status, setStatus] = useState('idle');

  useEffect(() => {
    // Load cached manifest (generated by Mac orchestration)
    const m = getCachedManifest(repo, `batches/mirror-merged/${dateFolder}`);
    setManifest(m);
  }, [repo, dateFolder]);

  const startTraining = async () => {
    setStatus('starting');
    try {
      // Pass CDN-only file list to Lightning training (via fetch to your backend or direct to Lightning SDK)
      // Example: send manifest file paths to backend which will launch Lightning Studio with CDN-only URLs
      const fileUrls = getDatasetFileUrls(repo, dateFolder);
      // Replace with your actual launcher call (e.g., POST /api/launch-training)
      const res = await fetch('/api/launch-training', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo, dateFolder, fileUrls, manifest }),
      });
      if (!res.ok) throw new Error('Launch failed');
      setStatus('launched');
    } catch (err) {
      console.error(err);
      setStatus('error');
    }
  };

  return (
    <div>
      <h2>Training
