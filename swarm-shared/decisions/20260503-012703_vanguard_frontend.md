# vanguard / frontend

## Final Consolidated Implementation

### 1. Diagnosis (merged)
- Frontend triggers authenticated `list_repo_tree` (via `/api/` proxy) on page/training load, burning HF quota (1000/5min) and causing 429s.
- File fetches use authenticated `/api/` or SDK paths instead of public CDN URLs, preventing CDN bypass.
- No persisted `(repo, dateFolder)` file-list cache; each visit re-enumerates folders and re-requests metadata.
- No client-side caching (ETag/Last-Modified or localStorage) for file-list; small changes force full API re-enumeration.
- No fallback when API quota is exhausted (UI shows hard error instead of using last-known file list + CDN fetches).
- Missing exponential backoff/retry on 429 with `Retry-After` header respect.

### 2. Proposed change (merged)
- **File scope**: `/opt/axentx/vanguard/src/lib/hf-client.js` (create or replace) — single source for HF interactions.
- Replace any `fetch(/api/...)` for file listing and file content with:
  - One-time `list_repo_tree` (from Mac orchestration) → save `public/file-list/{dataset}/{date}.json`.
  - Frontend loads that JSON (static import or fetch from `/file-list/...`) and constructs CDN URLs only.
  - Add lightweight retry/backoff for 429 with `Retry-After` header respect.
- Add client-side caching (localStorage + ETag/Last-Modified) for file-list and fallback to last-known manifest when quota is exhausted.
- Add UI fallback behavior: use last-known file list + CDN fetches when API fails.

### 3. Implementation (merged + corrected)

Create/replace `/opt/axentx/vanguard/src/lib/hf-client.js`:

```js
// src/lib/hf-client.js
// Lightweight HF CDN client: avoids /api/ auth paths, uses public CDN URLs.
// Expects a pre-generated file manifest at /file-list/{dataset}/{date}.json
// Format: { repo: "...", dateFolder: "...", files: ["path1", "path2", ...] }

const CDN_BASE = 'https://huggingface.co/datasets';
const MANIFEST_PATH = '/file-list'; // served statically from /public/file-list/

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchWithRetry(url, opts = {}, retries = 3) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(url, opts);
      if (res.status === 429) {
        const retryAfter = Number(res.headers.get('Retry-After')) || 60;
        await sleep(retryAfter * 1000);
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      return res;
    } catch (err) {
      lastErr = err;
      if (i < retries - 1) await sleep(1000 * 2 ** i);
    }
  }
  throw lastErr;
}

export function buildCdnUrl(repo, filePath, revision = 'main') {
  const encodedPath = filePath.split('/').map(encodeURIComponent).join('/');
  return `${CDN_BASE}/${repo}/resolve/${revision}/${encodedPath}`;
}

function getManifestCacheKey(dataset, dateFolder) {
  return `hf-manifest-${dataset}-${dateFolder}`;
}

function getCachedManifest(dataset, dateFolder) {
  try {
    const key = getManifestCacheKey(dataset, dateFolder);
    const cached = localStorage.getItem(key);
    if (!cached) return null;
    const { timestamp, etag, lastModified, manifest } = JSON.parse(cached);
    return { timestamp, etag, lastModified, manifest };
  } catch {
    return null;
  }
}

function setCachedManifest(dataset, dateFolder, manifest, etag = null, lastModified = null) {
  try {
    const key = getManifestCacheKey(dataset, dateFolder);
    localStorage.setItem(key, JSON.stringify({
      timestamp: Date.now(),
      etag,
      lastModified,
      manifest
    }));
  } catch {
    // ignore localStorage errors
  }
}

export async function loadFileManifest(dataset, dateFolder, { useCache = true, fallbackToCache = true } = {}) {
  const cacheKey = `${MANIFEST_PATH}/${dataset}/${dateFolder}.json`;
  const cached = useCache ? getCachedManifest(dataset, dateFolder) : null;

  const headers = {};
  if (cached?.etag) headers['If-None-Match'] = cached.etag;
  else if (cached?.lastModified) headers['If-Modified-Since'] = cached.lastModified;

  try {
    const res = await fetchWithRetry(cacheKey, { headers });
    if (res.status === 304 && cached) {
      return cached.manifest;
    }
    const etag = res.headers.get('ETag');
    const lastModified = res.headers.get('Last-Modified');
    const manifest = await res.json();
    setCachedManifest(dataset, dateFolder, manifest, etag, lastModified);
    return manifest;
  } catch (err) {
    if (fallbackToCache && cached) {
      return cached.manifest;
    }
    throw err;
  }
}

export async function fetchFileAsText(repo, filePath, revision = 'main') {
  const url = buildCdnUrl(repo, filePath, revision);
  const res = await fetchWithRetry(url);
  return res.text();
}

export async function fetchFileAsJson(repo, filePath, revision = 'main') {
  const url = buildCdnUrl(repo, filePath, revision);
  const res = await fetchWithRetry(url);
  return res.json();
}

export async function fetchFilesAsText(repo, filePaths, concurrency = 6) {
  const results = [];
  const executing = new Set();
  for (const fp of filePaths) {
    const p = fetchFileAsText(repo, fp).then((text) => {
      executing.delete(p);
      return { path: fp, text };
    });
    executing.add(p);
    results.push(p);
    if (executing.size >= concurrency) {
      await Promise.race(executing);
    }
  }
  return Promise.all(results);
}
```

Add a build/export step (or simple script) to generate manifests on the Mac orchestrator:

```bash
# scripts/generate-hf-manifest.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-username/dataset}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
SAFE_REPO="${REPO//\//-}"
OUT_DIR="/opt/axentx/vanguard/public/file-list/${SAFE_REPO}"
mkdir -p "$OUT_DIR"

# Use HF API once (after rate-limit window) to list a date folder.
# Save minimal manifest for frontend.
node - <<NODE
const { HfApi } = require('@huggingface/hub');
const api = new HfApi();
(async () => {
  try {
    const tree = await api.listRepoTree({ repo: { type: 'dataset', repo: '$REPO' }, path: '$DATE_FOLDER', recursive: false });
    const files = tree.filter((t) => t.type === 'file').map((t) => \`$DATE_FOLDER/\${t.path}\`);
    const manifest = { repo: '$REPO', dateFolder: '$DATE_FOLDER', files };
    require('fs').writeFileSync('$OUT_DIR/$DATE_FOLDER.json', JSON.stringify(manifest, null, 2));
    console.log('Manifest saved:', manifest.files.length, 'files');
  } catch (err) {
    console.error('Failed to list repo tree:', err.message);
    process.exit(1);
  }
})();
NODE
```

Make executable and ensure crontab uses Bash:

```bash
chmod +x scripts/generate-hf-manifest.sh
# In crontab (if used):
SHELL=/bin/bash
```

Update frontend usage (example):

```js
import { loadFileManifest, fetchFilesAsText } from '$lib/hf-client';

async function loadDatasetSlice(dataset, dateFolder) {
  const manifest = await loadFileManifest(dataset, dateFolder, { useCache: true, fallbackToCache: true });
  // Fetch subset or all files via
