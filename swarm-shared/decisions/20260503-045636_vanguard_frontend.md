# vanguard / frontend

## Final Synthesized Solution

### Diagnosis (merged, de-duplicated)
- Frontend triggers HF API `list_repo_tree`/`load_dataset` at runtime for dataset discovery → exposes UI and training to 429 rate limits, non-reproducible shard order, and quota burn.
- No content-addressed manifest per date folder → epochs drift across runs; resumable training cannot pin an exact snapshot.
- Dataset listing happens on every UI load/refresh instead of once per date folder and cached.
- Data loader uses Hugging Face `datasets` API during training instead of CDN-only fetches → burns API quota on every epoch/worker.
- Missing deterministic file list embedded at build time → training workers re-enumerate folders and pay API cost repeatedly.
- No fallback when API 429 occurs → UI/training stalls instead of using locally cached manifest + CDN bypass.

### Single Proposed Change (scope + artifacts)
- **Scope**: `/opt/axentx/vanguard`
- **Artifacts**:
  1. `/opt/axentx/vanguard/scripts/build-manifest.js` — build-time, one HF API call per date folder, produces deterministic, content-addressed manifest.
  2. `/opt/axentx/vanguard/src/frontend/utils/dataLoader.js` — runtime CDN-only loader; consumes manifest; zero Authorization/API calls.
  3. `/opt/axentx/vanguard/src/frontend/components/TrainingForm.jsx` — UI wiring to select date, load manifest, and launch training with pinned snapshot.
  4. `/opt/axentx/vanguard/scripts/launch-training.js` — Lightning launcher (or orchestrator script) that accepts CDN URLs and uses CDN-only parquet reader.
  5. Optional: `/opt/axentx/vanguard/src/frontend/utils/offlineFallback.js` — resilient fallback to last-known manifest + CDN when API 429 occurs.

### Implementation (concrete, actionable)

#### 1) Build-time manifest generator
Create `/opt/axentx/vanguard/scripts/build-manifest.js`:

```js
#!/usr/bin/env node
// Build manifest for a specific date folder using HF API once, then CDN-only.
// Usage: HF_TOKEN=... node scripts/build-manifest.js --repo org/vanguard-data --date 2026-05-03 --out manifests/2026-05-03/file-list.json

import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { program } from 'commander';
import { HfApi } from '@huggingface/hub';

program
  .requiredOption('--repo <repo>', 'HF dataset repo (org/repo)')
  .requiredOption('--date <date>', 'Date folder (YYYY-MM-DD)')
  .requiredOption('--out <file>', 'Output JSON file')
  .parse();

const opts = program.opts();
const api = new HfApi({ accessToken: process.env.HF_TOKEN });

async function main() {
  const folder = `batches/mirror-merged/${opts.date}`;
  console.log(`Listing ${opts.repo}/${folder} ...`);

  // Single API call per folder (non-recursive to reduce pagination)
  const tree = await api.listRepoTree({
    repo: opts.repo,
    path: folder,
    recursive: false,
  });

  // Keep only parquet files; produce CDN URLs and local slugs
  const files = (tree.filter((t) => t.type === 'file' && t.path.endsWith('.parquet')) || [])
    .map((f) => {
      const slug = f.path;
      const cdnUrl = `https://huggingface.co/datasets/${opts.repo}/resolve/main/${encodeURIComponent(slug)}`;
      return { slug, cdnUrl, size: f.size || 0 };
    })
    .sort((a, b) => a.slug.localeCompare(b.slug)); // deterministic order

  const manifest = {
    repo: opts.repo,
    date: opts.date,
    folder,
    createdAt: new Date().toISOString(),
    files,
    // content-addressable hash of the file list (simple deterministic hash)
    hash: hashManifest({ repo: opts.repo, date: opts.date, folder, files }),
  };

  const outDir = path.dirname(opts.out);
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(opts.out, JSON.stringify(manifest, null, 2));
  console.log(`Wrote ${files.length} files to ${opts.out}`);
}

function hashManifest(obj) {
  // Deterministic, non-crypto hash for content addressing (FNV-1a style)
  const str = JSON.stringify(obj, Object.keys(obj).sort());
  let h = 2166136261 >>> 0;
  for (let i = 0; i < str.length; i++) {
    h = Math.imul(h ^ str.charCodeAt(i), 16777619) >>> 0;
  }
  return h.toString(36);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build-manifest.js
```

#### 2) CDN-only data loader (frontend + training)
Update `/opt/axentx/vanguard/src/frontend/utils/dataLoader.js`:

```js
// Lightweight CDN-only loader that uses a pre-built manifest.
// Avoids HF datasets/list_repo_tree API calls at runtime.

export async function loadManifest(manifestPath) {
  const res = await fetch(manifestPath);
  if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
  return res.json();
}

export async function* streamParquetBatches(manifest, { batchSize = 32 } = {}) {
  // In browser/UI context this would typically fetch and parse parquet in workers.
  // For training-launcher integration we return CDN URLs for Lightning workers to fetch.
  for (const f of manifest.files) {
    yield {
      url: f.cdnUrl,
      slug: f.slug,
      size: f.size,
    };
  }
}

// For direct training script usage: produce a file with CDN URLs (one per line)
export function writeCdnUrlsFile(manifest, outPath) {
  const fs = require('fs');
  const lines = manifest.files.map((f) => f.cdnUrl).join('\n');
  fs.writeFileSync(outPath, lines);
}
```

#### 3) Offline/429 fallback (resilience)
Create `/opt/axentx/vanguard/src/frontend/utils/offlineFallback.js`:

```js
// Best-effort fallback to last-known manifest when API 429 occurs.
// Keeps a small local cache of recently loaded manifests indexed by date.

const CACHE_KEY = 'vanguard_manifest_cache_v1';

export function cacheManifest(date, manifest) {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    const cache = raw ? JSON.parse(raw) : {};
    cache[date] = { manifest, ts: Date.now() };
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch (e) {
    // ignore storage errors
  }
}

export function getCachedManifest(date) {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    const cache = raw ? JSON.parse(raw) : {};
    const entry = cache[date];
    return entry && entry.manifest;
  } catch (e) {
    return null;
  }
}
```

#### 4) Wire into training UI
Update `/opt/axentx/vanguard/src/frontend/components/TrainingForm.jsx`:

```jsx
import { useEffect, useState } from 'react';
import { loadManifest } from '../utils/dataLoader';
import { cacheManifest, getCachedManifest } from '../utils/offlineFallback';

export default function TrainingForm() {
  const [manifest, setManifest] = useState(null);
  const [loading, setLoading] = useState(false);
  const date = '2026-05-03'; // or selected by user
  const manifestPath = `/manifests/${date}/file-list.json`;

  useEffect(() => {
    setLoading(true);
    loadManifest(manifestPath)
      .then((m) => {
        setManifest(m);
        cacheManifest(date, m);
      })
