# vanguard / frontend

## Final synthesized solution

**Core problem**: training and UI hit HF API at runtime → 429s, non-reproducible runs, and schema pollution from mixed files.  
**Goal**: deterministic, CDN-only data loading + reproducible manifest + schema projection, with zero API calls during training.

---

### 1. Manifest generator (Mac/Linux orchestration)

Create `scripts/generate-manifest.ts` (run from your Mac during off-peak or CI; not in browser):

```ts
// scripts/generate-manifest.ts
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

const OWNER = process.env.HF_OWNER || 'your-org';
const REPO  = process.env.HF_REPO  || 'vanguard';
const OUT_DIR = path.resolve('manifests');

function hfApi<T>(path: string): T {
  const url = `https://huggingface.co/api${path}`;
  const headers = process.env.HF_TOKEN ? { Authorization: `Bearer ${process.env.HF_TOKEN}` } : {};
  const res = fetch(url, { headers }).then(r => {
    if (!r.ok) throw new Error(`HF API ${r.status} ${url}`);
    return r.json() as Promise<T>;
  });
  return res as T; // simplified sync-like usage in script context
}

// Use huggingface_hub for reliable recursive tree + ETag/SHA256
function listRepoTree(folder = ''): any[] {
  const out = execSync(
    `huggingface-cli repo tree ${OWNER}/${REPO} --revision main ${folder ? `--path ${folder}` : ''} --json`,
    { encoding: 'utf8' }
  );
  return JSON.parse(out).map((n: any) => ({
    path: n.path,
    type: n.type,
    size: n.size,
    lfs: n.lfs,
    sha256: n.lfs?.sha256 ?? undefined
  }));
}

export interface ManifestEntry {
  path: string;
  size: number;
  sha256?: string;
  cdnUrl: string;
}

export interface DatasetManifest {
  repo: string;
  owner: string;
  folder: string;
  generatedAt: string;
  files: ManifestEntry[];
}

export async function generateManifest(folder: string): Promise<DatasetManifest> {
  const items = listRepoTree(folder);
  const files: ManifestEntry[] = items
    .filter((i) => i.type === 'file' && i.path.endsWith('.parquet'))
    .map((i) => ({
      path: i.path,
      size: i.size,
      sha256: i.sha256,
      cdnUrl: `https://huggingface.co/datasets/${OWNER}/${REPO}/resolve/main/${i.path}`
    }));

  const manifest: DatasetManifest = {
    repo: REPO,
    owner: OWNER,
    folder,
    generatedAt: new Date().toISOString(),
    files
  };

  fs.mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, `${folder.replace(/\//g, '_')}.json`);
  fs.writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written: ${outPath} (${files.length} files)`);
  return manifest;
}

// CLI
if (require.main === module) {
  const folder = process.argv[2] || '2026-04-29';
  generateManifest(folder).catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
```

- Uses `huggingface_hub` CLI for robust recursive listing and SHA256 (more reliable than raw `/tree` API).  
- Run once per snapshot (e.g., `node scripts/generate-manifest.ts 2026-04-29`). Commit `manifests/*.json` or host statically.

---

### 2. Frontend manifest loader + picker (React/TypeScript)

`frontend/src/lib/manifest.ts`:

```ts
// frontend/src/lib/manifest.ts
export interface ManifestEntry {
  path: string;
  size: number;
  sha256?: string;
  cdnUrl: string;
}

export interface DatasetManifest {
  repo: string;
  owner: string;
  folder: string;
  generatedAt: string;
  files: ManifestEntry[];
}

// Deterministic repo picker for commit-cap spreading (5 siblings)
export function pickRepoBySlug(slug: string): string {
  const siblings = [
    'vanguard',
    'vanguard-sib1',
    'vanguard-sib2',
    'vanguard-sib3',
    'vanguard-sib4'
  ];
  const hash = Array.from(slug).reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0);
  return siblings[Math.abs(hash) % siblings.length];
}

// Load static manifest bundled at build time or fetch static copy
export async function loadManifest(folder: string): Promise<DatasetManifest> {
  // Try bundled first (if copied into public/ or built assets)
  const bundledPath = `/manifests/${folder.replace(/\//g, '_')}.json`;
  try {
    const r = await fetch(bundledPath, { cache: 'no-store' });
    if (r.ok) return r.json();
  } catch {
    // fallback
  }

  // Fallback to explicit fetch from CDN/static host
  const alt = `https://your-cdn.example.com/manifests/${folder.replace(/\//g, '_')}.json`;
  const r = await fetch(alt);
  if (!r.ok) throw new Error(`Manifest unavailable: ${r.status}`);
  return r.json();
}
```

`frontend/src/components/DatasetManifest.tsx` (React):

```tsx
// frontend/src/components/DatasetManifest.tsx
import React, { useState } from 'react';
import { loadManifest, type DatasetManifest } from '../lib/manifest';

export function DatasetManifest() {
  const [manifest, setManifest] = useState<DatasetManifest | null>(null);
  const [loading, setLoading] = useState(false);
  const [folder, setFolder] = useState('2026-04-29');

  async function handleGenerate() {
    setLoading(true);
    try {
      const m = await loadManifest(folder);
      setManifest(m);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="dataset-manifest">
      <h3>Dataset Manifest (CDN-bypass)</h3>
      <div style={{ marginBottom: 8 }}>
        <input
          value={folder}
          onChange={(e) => setFolder(e.target.value)}
          placeholder="YYYY-MM-DD"
        />
        <button onClick={handleGenerate} disabled={loading} style={{ marginLeft: 8 }}>
          {loading ? 'Loading...' : 'Load Manifest'}
        </button>
      </div>

      {manifest && (
        <>
          <div className="meta" style={{ marginBottom: 8, color: '#666' }}>
            <small>
              Repo: {manifest.owner}/{manifest.repo} | Folder: {manifest.folder} | Files:{' '}
              {manifest.files.length}
            </small>
            <br />
            <small>Generated: {manifest.generatedAt}</small>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={{ border: '1px solid #ddd', padding: 8, textAlign: 'left' }}>File</th>
                <th style={{ border: '1px solid #ddd', padding: 8, textAlign: 'left' }}>Size</th>
                <th style={{ border: '1px solid #ddd', padding: 8, textAlign: 'left' }}>CDN URL</th>
              </tr>
            </thead>
            <tbody>
              {manifest.files.map((f) => (
                <tr key={f.path}>
                  <td style={{ border
