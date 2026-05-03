# vanguard / frontend

# Final Synthesized Solution

## Diagnosis (merged + corrected)
- **Repeated authenticated enumeration**: every training launch re-enumerates via HF API → quota burn + 429 risk.
- **Late, expensive failures**: UI cannot pre-flight or cache available files; users pick invalid/missing/incompatible folders and discover errors only at launch or during training.
- **Schema/format risk**: training likely uses `load_dataset(streaming=True)` on heterogeneous repos → pyarrow `CastError` on mixed schemas; no client-side validation that selected folder contains expected parquet files.
- **No CDN bypass**: training still relies on authenticated API during data loading instead of using `resolve/main/` CDN URLs; no CDN-only file list to enable zero-API data loads in Lightning jobs.
- **No local cache/TTL**: identical folder listings repeat across sessions and users; no artifact to embed in training jobs.

## Single Change: “Generate & Cache CDN Manifest”
Add a frontend flow + minimal backend to:
1. List a `(repo, dateFolder)` **once** (server-side with HF token).
2. Validate that listed files are parquet (or expected extensions) and deduplicate.
3. Persist a manifest containing CDN URLs to `/manifests/`.
4. Embed CDN URLs in the launch payload so training can consume data without authenticated API calls.
5. Add TTL (24 h) and per-`(repo,dateFolder)` caching to prevent re-enumeration and reduce quota use.

---

## Implementation

### 1. CDN helper (no-auth URL builder)
```ts
// src/lib/hf-cdn.ts
export function cdnDatasetFileUrl(repo: string, filePath: string): string {
  // Public CDN URL — no Authorization header required
  return `https://huggingface.co/datasets/${repo}/resolve/main/${filePath}`;
}

export function buildCdnManifest(files: string[], repo: string) {
  return files.map((f) => ({
    path: f,
    url: cdnDatasetFileUrl(repo, f),
    size: null
  }));
}
```

### 2. Manifest cache layer (frontend)
```ts
// src/features/training/FileManifestCache.ts
import { buildCdnManifest } from '../lib/hf-cdn';

const MANIFEST_DIR = '/manifests';
const TTL_MS = 24 * 60 * 60 * 1000; // 24h

export interface ManifestEntry {
  path: string;
  url: string;
  size: number | null;
}

export interface FileManifest {
  repo: string;
  dateFolder: string;
  generatedAt: string;
  files: ManifestEntry[];
}

function isExpired(manifest: FileManifest): boolean {
  return Date.now() - new Date(manifest.generatedAt).getTime() > TTL_MS;
}

export async function saveManifest(repo: string, dateFolder: string, files: string[]): Promise<FileManifest> {
  // Keep only parquet files (schema expectation) and deduplicate
  const parquetFiles = Array.from(new Set(files.filter((f) => f.endsWith('.parquet'))));
  const manifest: FileManifest = {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files: buildCdnManifest(parquetFiles, repo)
  };

  const res = await fetch('/api/manifests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(manifest)
  });

  if (!res.ok) throw new Error('Failed to save manifest');
  return manifest;
}

export async function loadManifest(repo: string, dateFolder: string): Promise<FileManifest | null> {
  const res = await fetch(
    `/api/manifests?repo=${encodeURIComponent(repo)}&dateFolder=${encodeURIComponent(dateFolder)}`
  );
  if (!res.ok) return null;
  const manifest = (await res.json()) as FileManifest;
  return isExpired(manifest) ? null : manifest;
}
```

### 3. Minimal backend endpoints (Node/Express)
```ts
// src/routes/manifests.ts
import express from 'express';
import fs from 'fs';
import path from 'path';
import { FileManifest } from '../features/training/FileManifestCache';

const router = express.Router();
const MANIFEST_DIR = path.resolve(process.cwd(), 'manifests');

if (!fs.existsSync(MANIFEST_DIR)) fs.mkdirSync(MANIFEST_DIR, { recursive: true });

router.post('/', (req, res) => {
  const manifest: FileManifest = req.body;
  const safeRepo = manifest.repo.replace(/[/\\]/g, '_');
  const filename = `manifest-${safeRepo}-${manifest.dateFolder}.json`;
  fs.writeFileSync(path.join(MANIFEST_DIR, filename), JSON.stringify(manifest, null, 2));
  res.json({ ok: true });
});

router.get('/', (req, res) => {
  const { repo, dateFolder } = req.query as { repo?: string; dateFolder?: string };
  if (!repo || !dateFolder) return res.status(400).json({ error: 'repo and dateFolder required' });
  const safeRepo = repo.replace(/[/\\]/g, '_');
  const filename = `manifest-${safeRepo}-${dateFolder}.json`;
  const filepath = path.join(MANIFEST_DIR, filename);
  if (!fs.existsSync(filepath)) return res.status(404).json({ error: 'not found' });
  res.json(JSON.parse(fs.readFileSync(filepath, 'utf8')));
});

export default router;
```

### 4. Server-side tree lister (single authenticated call)
```ts
// src/api/hf-list-tree.ts (server route)
import { HuggingFaceApi } from '../lib/hf-api';
import express from 'express';

const router = express.Router();

router.post('/list-tree', async (req, res) => {
  const { repo, path: folderPath, recursive = false } = req.body;
  try {
    // Server-side call using stored HF token; enumerates once per (repo,folder)
    const tree = await HuggingFaceApi.listRepoTree(repo, { path: folderPath, recursive });
    // Return only files (not directories)
    const files = tree
      .filter((entry) => entry.type === 'file')
      .map((entry) => entry.path)
      .filter(Boolean);
    res.json({ files });
  } catch (err) {
    console.error('HF list-tree failed', err);
    res.status(502).json({ error: 'Failed to list repo tree' });
  }
});

export default router;
```

### 5. Updated training launch form (frontend)
```tsx
// src/features/training/TrainingLaunchForm.tsx
import { useState } from 'react';
import { loadManifest, saveManifest } from './FileManifestCache';

export function TrainingLaunchForm({
  repo,
  dateFolder,
  onLaunch
}: {
  repo: string;
  dateFolder: string;
  onLaunch: (files: string[]) => void;
}) {
  const [manifest, setManifest] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);

  async function generateManifest() {
    setGenerating(true);
    try {
      // Single server-side list call (authenticated)
      const res = await fetch('/api/hf/list-tree', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo, path: dateFolder, recursive: false })
      });
      if (!res.ok) throw new Error('List failed');
      const { files } = await res.json(); // { files: string[] }
      if (!files.length) throw new Error('No files found in folder');
      const m = await saveManifest(repo, dateFolder, files);
      setManifest(m);
    } catch (e) {
      console.error(e);
    } finally {
      setGenerating(false);
    }
  }

  async function loadCached() {
    setLoading(true);
    const m = await loadManifest(repo, dateFolder);
    setManifest(m);
    setLoading(false);
  }

  function launchWithCdn() {
    if (!manifest || !manifest.files.length) return;
    // Pass
