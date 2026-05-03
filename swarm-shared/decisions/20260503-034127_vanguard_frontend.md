# vanguard / frontend

## Final synthesized solution (one coherent plan)

**Core problem**: Frontend still makes live HF API calls (`list_repo_tree`, `load_dataset`) during dataset selection/preview → 429s, non-reproducible runs, slow/blocking UX.  
**Goal**: Deterministic, CDN-first dataset access with a single offline-capable manifest generated once (Mac orchestrator) and shipped with the frontend.

---

## 1. Architecture (CDN-first + manifest)

- **Deterministic manifest**: `public/manifest-{date}.json` (committed/generated) maps `{date}/{slug} → repo-relative path + metadata`.  
- **Frontend never calls `list_repo_tree`/`load_dataset` at runtime** for selection/preview.  
- **All previews served via CDN** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `Range` requests and `force-cache`.  
- **Graceful fallback**: if manifest missing, show cached/empty state and surface actionable error (do not silently fail or hit live API).  
- **Build/CI step**: generate manifest on Mac orchestrator with one API call, commit/upload alongside frontend assets.

---

## 2. Files to add/modify

### `/opt/axentx/vanguard/frontend/src/lib/manifest.ts`
```ts
// Deterministic manifest loader (CDN-first). Prefers local static manifest.
// Avoids runtime HF API enumeration entirely in production.

export type ManifestEntry = {
  date: string;   // YYYY-MM-DD
  slug: string;   // filename slug (without extension)
  path: string;   // repo-relative path
  size: number;
  sha256?: string;
};

export type Manifest = {
  repo: string;
  generatedAt: string;
  files: ManifestEntry[];
};

const CDN_ROOT = 'https://huggingface.co/datasets';

export class ManifestStore {
  private cache: Manifest | null = null;

  constructor(
    private repo: string,
    private manifestPath?: string // e.g. /manifest-2026-05-03.json
  ) {}

  async load(): Promise<Manifest> {
    if (this.cache) return this.cache;

    // 1) Prefer static manifest (CDN, no auth, cacheable)
    if (this.manifestPath) {
      try {
        const res = await fetch(this.manifestPath, { cache: 'force-cache' });
        if (res.ok) {
          const json = (await res.json()) as Manifest;
          // Basic validation
          if (json && Array.isArray(json.files) && json.repo) {
            this.cache = json;
            return this.cache;
          }
        }
      } catch {
        // fallback to error state below
      }
    }

    // 2) Dev fallback: single cached tree call (avoid in production).
    // Production deployments must provide a static manifest.
    const apiUrl = `https://huggingface.co/api/datasets/${this.repo}/tree`;
    const res = await fetch(apiUrl, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
    const tree = await res.json();

    const files: ManifestEntry[] = (Array.isArray(tree) ? tree : []).map((node: any) => {
      const path = node.path || '';
      const parts = path.split('/');
      const date = parts[0] || '';
      const slug = parts[1] ? parts[1].replace(/\.[^/.]+$/, '') : parts[1] || '';
      return {
        date,
        slug,
        path,
        size: node.size || 0,
        sha256: node.lfs?.oid || undefined,
      };
    });

    this.cache = { repo: this.repo, generatedAt: new Date().toISOString(), files };
    return this.cache;
  }

  toCDNUrl(entry: ManifestEntry): string {
    return `${CDN_ROOT}/${this.repo}/resolve/main/${encodeURIComponent(entry.path)}`;
  }

  findByDateSlug(date: string, slug: string): ManifestEntry | undefined {
    return this.cache?.files.find((f) => f.date === date && f.slug === slug);
  }
}
```

### `/opt/axentx/vanguard/frontend/src/lib/cdn.ts`
```ts
import type { ManifestEntry } from './manifest';

export async function fetchCDNPreview(
  repo: string,
  entry: ManifestEntry,
  maxBytes = 65536
): Promise<{ text?: string; hex?: string; truncated: boolean } | null> {
  const url = `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(entry.path)}`;
  try {
    const res = await fetch(url, {
      headers: { Range: `bytes=0-${maxBytes - 1}` },
      cache: 'force-cache',
    });
    if (!res.ok && res.status !== 206 && res.status !== 200) return null;
    const buf = await res.arrayBuffer();
    const arr = new Uint8Array(buf);
    const truncated = arr.length >= maxBytes;

    // Try UTF-8 text preview
    try {
      const text = new TextDecoder().decode(arr);
      return { text, truncated };
    } catch {
      // Binary -> hex preview
      const hex = Array.from(arr)
        .map((b) => b.toString(16).padStart(2, '0'))
        .join(' ')
        .slice(0, 400);
      return { hex, truncated };
    }
  } catch {
    return null;
  }
}
```

### `/opt/axentx/vanguard/frontend/src/lib/useDatasetFiles.ts`
```ts
import { useEffect, useState } from 'react';
import { ManifestStore, type ManifestEntry } from './manifest';

export function useDatasetFiles(repo: string, manifestPath?: string) {
  const [store] = useState(() => new ManifestStore(repo, manifestPath));
  const [files, setFiles] = useState<ManifestEntry[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    store
      .load()
      .then((m) => setFiles(m.files))
      .catch((e) => setError(e.message || 'Failed to load dataset manifest'))
      .finally(() => setLoading(false));
  }, [store]);

  return { files, loading, error };
}
```

### `/opt/axentx/vanguard/scripts/gen-manifest.js`
```js
#!/usr/bin/env node
// Generate static manifest for a dataset repo and date folder.
// Usage: node gen-manifest.js --repo=owner/dataset --date=2026-05-03 --out=public/manifest-2026-05-03.json
// Run from orchestration/Mac (single API call), then commit/upload.

const https = require('https');
const fs = require('fs');
const path = require('path');

function getArg(name, envName) {
  const arg = process.argv.find((a) => a.startsWith(`--${name}=`));
  if (arg) return arg.split('=')[1];
  if (envName && process.env[envName]) return process.env[envName];
  return undefined;
}

const repo = getArg('repo', 'HF_REPO');
const date = getArg('date', 'HF_DATE');
const out = getArg('out') || path.join('public', `manifest-${date}.json`);

if (!repo || !date) {
  console.error('Usage: node gen-manifest.js --repo=owner/dataset --date=YYYY-MM-DD [--out=path]');
  process.exit(1);
}

function apiGet(treePath = '') {
  const url = `https://huggingface.co/api/datasets/${repo}/tree/${encodeURIComponent(treePath)}`;
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: 30000 }, (res) => {
      let data = '';
      res.on('data', (chunk) => (data += chunk));
      res
