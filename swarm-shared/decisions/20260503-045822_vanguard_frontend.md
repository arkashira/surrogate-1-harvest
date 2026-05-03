# vanguard / frontend

## Final Synthesized Solution

### 1. Diagnosis (merged)
- **Root cause**: No content-addressed manifest per date folder → training relies on runtime `list_repo_tree`/`load_dataset`, causing HF API 429s and non-reproducible runs.
- **UI gaps**: No snapshot selector, no manifest generation UI, no visual “pinned” indicator, and no local preview of date-partitioned parquet lists.
- **Orchestration mismatch**: Manual JSON creation and Mac-only scripts break separation of concerns (orchestration should own API calls; training should use CDN-only file lists).
- **Reliability gaps**: No validation that manifest matches remote folder and no lightweight feedback when generation starts/completes.

### 2. Implementation Plan (merged + corrected)

#### 2.1 Manifest generator (Node utility)
Path: `/opt/axentx/vanguard/src/features/manifest/generateManifest.js`

```js
// src/features/manifest/generateManifest.js
// Runs in Node (frontend build/ops) — not bundled to browser.
import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'fs';
import { join } from 'path';

const API_ROOT = 'https://huggingface.co';
const REPO = process.env.HF_DATASET_REPO || 'datasets/your-org/surrogate-1';
const OUT_DIR = process.env.MANIFEST_OUT_DIR || 'public/manifests';

async function listRepoTree(path = '') {
  const url = `${API_ROOT}/api/datasets/${REPO}/tree/${encodeURIComponent(path)}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${process.env.HF_API_TOKEN || ''}` }
  });
  if (res.status === 429) {
    const retryAfter = Number(res.headers.get('retry-after')) || 360;
    throw new Error(`HF 429 — retry after ${retryAfter}s`);
  }
  if (!res.ok) throw new Error(`HF tree failed: ${res.status}`);
  return res.json();
}

function buildManifest(tree, dateFolder) {
  const files = (tree || [])
    .filter((f) => f.type === 'file' && f.path.endsWith('.parquet'))
    .map((f) => ({
      path: f.path,
      size: f.size,
      lfs: f.lfs?.oid || null,
      cdn: `${API_ROOT}/datasets/${REPO}/resolve/main/${encodeURIComponent(f.path)}`
    }))
    .sort((a, b) => a.path.localeCompare(b.path));

  const manifest = {
    generatedAt: new Date().toISOString(),
    repo: REPO,
    dateFolder,
    snapshotId: require('crypto')
      .createHash('sha256')
      .update(JSON.stringify(files.map((f) => `${f.path}:${f.size}`)))
      .digest('hex')
      .slice(0, 16),
    count: files.length,
    files
  };
  return manifest;
}

export async function generateManifest(dateFolder, { force = false } = {}) {
  if (!dateFolder) throw new Error('dateFolder required (e.g. 2026-05-03)');
  console.log(`Listing ${REPO}/${dateFolder} ...`);
  const tree = await listRepoTree(dateFolder);
  const manifest = buildManifest(tree, dateFolder);

  if (!existsSync(OUT_DIR)) mkdirSync(OUT_DIR, { recursive: true });
  const outPath = join(OUT_DIR, `${dateFolder}.json`);

  // Validate freshness: if exists and not forced, compare snapshotId
  if (!force && existsSync(outPath)) {
    const existing = JSON.parse(readFileSync(outPath, 'utf8'));
    if (existing.snapshotId === manifest.snapshotId) {
      console.log(`Manifest up-to-date: ${outPath} (snapshot=${manifest.snapshotId})`);
      return manifest;
    }
    console.log(`Manifest changed — regenerating (old=${existing.snapshotId}, new=${manifest.snapshotId})`);
  }

  writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written: ${outPath} (snapshot=${manifest.snapshotId}, files=${manifest.count})`);

  // Regenerate index.json
  await writeIndex();
  return manifest;
}

async function writeIndex() {
  const files = readdirSync(OUT_DIR).filter((f) => f.endsWith('.json') && f !== 'index.json');
  const index = files.map((f) => {
    const content = JSON.parse(readFileSync(join(OUT_DIR, f), 'utf8'));
    return { dateFolder: content.dateFolder, snapshotId: content.snapshotId, generatedAt: content.generatedAt };
  }).sort((a, b) => b.dateFolder.localeCompare(a.dateFolder));
  writeFileSync(join(OUT_DIR, 'index.json'), JSON.stringify(index, null, 2));
}

// CLI
if (import.meta.url === `file://${process.argv[1]}`) {
  const dateFolder = process.argv[2];
  const force = process.argv.includes('--force');
  generateManifest(dateFolder, { force }).catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

#### 2.2 Ops helper (Mac-friendly)
Path: `/opt/axentx/vanguard/scripts/gen-manifest.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: HF_API_TOKEN=hf_xxx HF_DATASET_REPO=datasets/your-org/surrogate-1 ./gen-manifest.sh 2026-05-03 [--force]
cd /opt/axentx/vanguard
node src/features/manifest/generateManifest.js "$1" --force="${4:-false}"
```
Make executable: `chmod +x scripts/gen-manifest.sh`

#### 2.3 Manifest API helper (frontend)
Path: `/opt/axentx/vanguard/src/features/manifest/manifestApi.js`

```js
// src/features/manifest/manifestApi.js
export async function listAvailableManifests() {
  const res = await fetch('/manifests/index.json').catch(() => ({ ok: false }));
  if (res?.ok) return res.json();
  // Fallback: try recent date folders
  const candidates = ['2026-05-03', '2026-05-02', '2026-05-01'];
  const out = [];
  for (const c of candidates) {
    const r = await fetch(`/manifests/${c}.json`).catch(() => null);
    if (r?.ok) out.push(await r.json());
  }
  return out;
}

export async function getManifest(dateFolder) {
  const res = await fetch(`/manifests/${dateFolder}.json`);
  if (!res.ok) throw new Error(`Manifest not found: ${dateFolder}`);
  return res.json();
}
```

#### 2.4 Training launcher UI (with pinned snapshot)
Path: `/opt/axentx/vanguard/src/features/training/TrainingLauncher.jsx`

```jsx
// src/features/training/TrainingLauncher.jsx
import { useState, useEffect } from 'react';
import { listAvailableManifests } from '../manifest/manifestApi';

export default function TrainingLauncher({ onLaunch }) {
  const [manifests, setManifests] = useState([]);
  const [snapshot, setSnapshot] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    listAvailableManifests()
      .then(setManifests)
      .catch(console.warn)
      .finally(() => setLoading(false));
  }, []);

  function handleLaunch() {
    const selected = manifests.find((m) => m.snapshotId === snapshot);
    onLaunch({
      dataset: {
        manifestUrl: selected ? `/manifests/${selected.dateFolder}.json` : null,
        files: selected ? selected.files : []
      }
    });
  }

  return (
    <div>
      <label>
        Snapshot (pinned manifest):
        <select value={snapshot} onChange={(e) => setSnapshot(e.target.value)} disabled={loading}>
          <option value="">(latest — not
