# vanguard / frontend

## Final Synthesized Implementation

**Diagnosis (merged, corrected):**
- Repeated `list_repo_tree` and dataset metadata calls will trigger HF API 429s (1000 req/5min for API; 128 commits/hr/repo for writes).
- No reuse of running Lightning Studio causes quota burn via redundant create/idle/stop cycles.
- Frontend previews that ingest raw HF files without schema projection risk parse failures and training ingestion errors.
- Frontend data layer still routes through authenticated `/api/` endpoints instead of public CDN, amplifying rate-limit exposure.
- No deterministic repo selection for writes exposes single-repo commit cap during iterative saves.

**Proposed change (merged):**
Add a frontend data layer and browser UI that:
- Caches repo file manifests once per repo+path (30–60m TTL) to eliminate repeated tree calls.
- Uses HF public CDN (`resolve/main/...`) for file content, bypassing authenticated endpoints.
- Projects only `{prompt,response}` from JSON/JSONL to prevent schema drift.
- Reuses a running Lightning Studio for preview/training actions instead of creating new ones.
- Deterministically selects a sibling repo for writes to spread load and avoid 128/hr cap.

**Files to create/modify:**
- `/opt/axentx/vanguard/src/frontend/lib/hf.ts`
- `/opt/axentx/vanguard/src/frontend/lib/lightningStudio.ts`
- `/opt/axentx/vanguard/src/frontend/lib/datasetBrowser.ts`
- `/opt/axentx/vanguard/src/frontend/components/DatasetBrowser.tsx`
- `/opt/axentx/vanguard/src/frontend/App.tsx` (or equivalent) — mount browser and wire actions.

---

### lib/hf.ts
```ts
// /opt/axentx/vanguard/src/frontend/lib/hf.ts
const HF_CDN_ROOT = 'https://huggingface.co';
const HF_API_ROOT = 'https://huggingface.co/api';

export interface RepoFile {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

export async function listRepoTree(repo: string, path = ''): Promise<RepoFile[]> {
  const url = `${HF_API_ROOT}/datasets/${repo}/tree${path ? `/${encodeURIComponent(path)}` : ''}?recursive=false`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HF tree failed: ${res.status}`);
  return res.json();
}

export function cdnFileUrl(repo: string, filePath: string): string {
  return `${HF_CDN_ROOT}/datasets/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

export async function fetchCdnFile(repo: string, filePath: string): Promise<string> {
  const url = cdnFileUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
  return res.text();
}

// Deterministic sibling repo selector to avoid 128/hr cap on single repo
export function pickWriteRepo(baseRepo: string, slug: string, siblingCount = 5): string {
  const [org, name] = baseRepo.split('/');
  if (!org || !name) return baseRepo;
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = (Math.imul(31, hash) + slug.charCodeAt(i)) | 0;
  }
  const idx = Math.abs(hash) % siblingCount;
  if (idx === 0) return baseRepo;
  return `${org}/${name}-s${idx}`;
}
```

### lib/lightningStudio.ts
```ts
// /opt/axentx/vanguard/src/frontend/lib/lightningStudio.ts
// Uses Lightning SDK available in the Studio build/runtime environment.

export async function findRunningStudio(name: string) {
  // @ts-ignore - Lightning SDK available at runtime
  const { Teamspace } = await import('lightning');
  const studios = await Teamspace.studios();
  for (const s of studios) {
    if (s.name === name && s.status === 'Running') return s;
  }
  return null;
}

export async function getOrCreateStudio(
  name: string,
  options: { cloud?: string; machine?: string } = {}
) {
  const existing = await findRunningStudio(name);
  if (existing) return existing;

  // @ts-ignore
  const { Studio } = await import('lightning');
  const cloud = options.cloud || 'lightning-public-prod';
  const machine = options.machine || 'L40S';
  return Studio.create({
    name,
    cloud,
    machine,
    create_ok: true,
  });
}
```

### lib/datasetBrowser.ts
```ts
// /opt/axentx/vanguard/src/frontend/lib/datasetBrowser.ts
import { listRepoTree, fetchCdnFile } from './hf';

export interface DatasetPreview {
  path: string;
  preview: { prompt?: string; response?: string } | null;
  error?: string;
}

export class DatasetBrowser {
  private cache = new Map<string, { files: string[]; ts: number }>();
  private ttl = 1000 * 60 * 30; // 30m

  async listFiles(repo: string, path = ''): Promise<string[]> {
    const key = `${repo}:${path}`;
    const cached = this.cache.get(key);
    if (cached && Date.now() - cached.ts < this.ttl) return cached.files;

    const tree = await listRepoTree(repo, path);
    const files = tree.filter((f) => f.type === 'file').map((f) => f.path);
    this.cache.set(key, { files, ts: Date.now() });
    return files;
  }

  // Project only {prompt,response} to avoid schema drift from heterogeneous HF files
  async previewFile(repo: string, filePath: string): Promise<DatasetPreview> {
    try {
      const text = await fetchCdnFile(repo, filePath);
      const lines = text.split('\n').filter(Boolean);
      const samples: Array<{ prompt?: string; response?: string }> = [];

      for (const line of lines.slice(0, 3)) {
        try {
          const obj = JSON.parse(line);
          samples.push({
            prompt: obj.prompt ?? obj.input ?? obj.question ?? undefined,
            response: obj.response ?? obj.output ?? obj.answer ?? undefined,
          });
        } catch {
          // skip non-JSON lines
        }
      }

      return { path: filePath, preview: samples[0] || null };
    } catch (err) {
      return { path: filePath, preview: null, error: String(err) };
    }
  }
}
```

### components/DatasetBrowser.tsx
```tsx
// /opt/axentx/vanguard/src/frontend/components/DatasetBrowser.tsx
import React, { useEffect, useState } from 'react';
import { DatasetBrowser as DatasetBrowserEngine } from '../lib/datasetBrowser';
import { getOrCreateStudio } from '../lib/lightningStudio';

const engine = new DatasetBrowserEngine();

export function DatasetBrowser({ repo }: { repo: string }) {
  const [files, setFiles] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [studioLoading, setStudioLoading] = useState(false);

  useEffect(() => {
    engine.listFiles(repo).then(setFiles).catch(console.error);
  }, [repo]);

  const loadPreview = async (path: string) => {
    setLoading(true);
    setSelected(path);
    const result = await engine.previewFile(repo, path);
    setPreview(result);
    setLoading(false);
  };

  const runInStudio = async () => {
    if (!selected) return;
    setStudioLoading(true);
    try {
      const studio = await getOrCreateStudio(`${repo}-preview`, {
        cloud: 'lightning-public-prod',
        machine: 'L40S',
      });
      // Example: open studio or trigger notebook/training with selected file
