# vanguard / frontend

## 1. Diagnosis
- No persisted file manifest on the frontend → repeated HF API `list_repo_tree` calls during UI interactions trigger 429 rate limits (1000 req/5 min).
- Frontend recomputes dataset file lists on mount/navigation instead of reading a local JSON manifest → slow loads and quota burn.
- Missing lightweight caching layer (IndexedDB/localStorage) for file manifests → offline/retry UX is brittle.
- No fallback to CDN-only paths when API is throttled → training/preview flows block on auth-check endpoints.
- Hardcoded or volatile file paths in UI components → brittle when HF repo structure changes.

## 2. Proposed change
Add a frontend file-manifest cache with CDN-first resolution:
- Create `src/lib/hf-manifest.ts` — single source for listing/caching repo file paths.
- Create `src/lib/cdn.ts` — helpers to build CDN URLs and fetch with timeout/retry.
- Update dataset browser component(s) to use the manifest instead of inline `list_repo_tree` calls.
- Add a build/startup script to pre-generate `public/manifest/{repo}/{date}.json` from the Mac orchestration step (so Lightning training can embed the same list).

Scope: add 2 new files, update 1–2 dataset-related frontend components, add one preload script.

## 3. Implementation

### File: `src/lib/cdn.ts`
```ts
// src/lib/cdn.ts
const HF_CDN = 'https://huggingface.co/datasets';

export function cdnUrl(repo: string, path: string): string {
  return `${HF_CDN}/${repo}/resolve/main/${path}`;
}

export async function fetchCdnText(repo: string, path: string, timeoutMs = 8000): Promise<string> {
  const url = cdnUrl(repo, path);
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) throw new Error(`CDN fetch ${res.status}`);
    return await res.text();
  } finally {
    clearTimeout(id);
  }
}
```

### File: `src/lib/hf-manifest.ts`
```ts
// src/lib/hf-manifest.ts
import { cdnUrl } from './cdn';

const MANIFEST_TTL_MS = 24 * 60 * 60 * 1000; // 1 day

interface FileNode {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

interface ManifestMeta {
  repo: string;
  folder: string;
  ts: number;
  files: string[];
}

const STORAGE_KEY = 'hf-manifest-v1';

function getStored(): Record<string, ManifestMeta> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function setStored(key: string, meta: ManifestMeta) {
  const all = getStored();
  all[key] = meta;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  } catch {
    // ignore quota/incognito failures
  }
}

function isFresh(meta: ManifestMeta): boolean {
  return Date.now() - meta.ts < MANIFEST_TTL_MS;
}

export async function getRepoFolderFiles(
  repo: string,
  folder: string,
  options?: {
    skipCache?: boolean;
    apiListUrl?: string; // optional endpoint on your backend to list once (Mac orchestration)
  }
): Promise<string[]> {
  const key = `${repo}:${folder}`;
  const stored = getStored();
  const cached = stored[key];

  if (!options?.skipCache && cached && isFresh(cached)) {
    return cached.files;
  }

  // Try to fetch pre-generated manifest from public folder (fast, no auth)
  try {
    const publicManifestUrl = `/manifest/${repo}/${encodeURIComponent(folder)}.json`;
    const r = await fetch(publicManifestUrl, { cache: 'no-cache' });
    if (r.ok) {
      const json = (await r.json()) as string[];
      setStored(key, { repo, folder, ts: Date.now(), files: json });
      return json;
    }
  } catch {
    // fallback to API or empty
  }

  // If backend provides a single list endpoint (recommended), use it once per folder
  if (options?.apiListUrl) {
    try {
      const r = await fetch(options.apiListUrl);
      if (r.ok) {
        const json = (await r.json()) as string[];
        setStored(key, { repo, folder, ts: Date.now(), files: json });
        return json;
      }
    } catch {
      // fallback
    }
  }

  // Last resort: client-side HF API (may 429) — keep minimal
  try {
    // Note: This call may be rate-limited. Prefer CDN/public manifest.
    const res = await fetch(
      `https://huggingface.co/api/datasets/${repo}/tree?path=${encodeURIComponent(folder)}&recursive=false`
    );
    if (!res.ok) throw new Error('HF tree API failed');
    const tree = (await res.json()) as FileNode[];
    const files = tree.filter((n) => n.type === 'file').map((n) => n.path);
    setStored(key, { repo, folder, ts: Date.now(), files });
    return files;
  } catch {
    return [];
  }
}

export function buildDatasetCdnUrls(repo: string, files: string[]): string[] {
  return files.map((f) => cdnUrl(repo, f));
}
```

### File: `src/components/DatasetBrowser.tsx` (example update)
```tsx
// src/components/DatasetBrowser.tsx
import { useEffect, useState } from 'react';
import { getRepoFolderFiles, buildDatasetCdnUrls } from '../lib/hf-manifest';

export function DatasetBrowser({ repo = 'your-org/vanguard-data', folder = 'batches/mirror-merged' }) {
  const [files, setFiles] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    getRepoFolderFiles(repo, folder, {
      // optional: point to a lightweight backend endpoint that returns the pre-generated list
      // apiListUrl: `/api/manifest/${repo}/${folder}`
    })
      .then(setFiles)
      .finally(() => setLoading(false));
  }, [repo, folder]);

  const urls = buildDatasetCdnUrls(repo, files);

  if (loading) return <div className="p-4 text-sm text-gray-500">Loading dataset index...</div>;

  return (
    <div className="space-y-2">
      {urls.map((url) => (
        <div key={url} className="p-2 border rounded text-xs">
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
            {url.replace(/^https?:\/\/[^\/]+\//, '')}
          </a>
        </div>
      ))}
    </div>
  );
}
```

### Build/startup helper: `scripts/generate-manifest.js`
```js
// scripts/generate-manifest.js
// Run on Mac orchestration (once per folder/date) to produce public/manifest/*.json
// Usage: node scripts/generate-manifest.js --repo org/vanguard-data --folder batches/mirror-merged/2026-05-02 --out public/manifest
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

function usage() {
  console.error('Usage: node generate-manifest.js --repo <repo> --folder <folder> --out <outDir>');
  process.exit(1);
}

const args = process.argv.slice(2).reduce((acc, cur) => {
  const [k, v] = cur.replace(/^--/, '').split('=');
  acc[k] = v;
  return acc;
}, {});

if (!args.repo || !args.folder ||
