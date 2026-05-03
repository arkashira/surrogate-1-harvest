# vanguard / frontend

## 1. Diagnosis
- Frontend has no content-addressed manifest for surrogate-1 date folders → training epochs drift and resumable runs are unreliable.
- Data loader still resolves files via HF API (`list_repo_tree`/`load_dataset`) at runtime → exposes surrogate-1 to 429 rate limits and non-reproducible shard order.
- No CDN-only fetch path in frontend tooling → misses HF CDN bypass (public `resolve/main/` URLs) that avoids auth/rate limits.
- Missing deterministic repo-selector for commit-cap mitigation (hash-slug → sibling repo) in frontend upload flow.
- No lightweight validation that file-list JSON embedded in training artifacts matches available CDN files before training starts.

## 2. Proposed change
Add a frontend build/utility module that:
- Generates a content-addressed manifest (`file-list.json`) for a single date folder under `batches/mirror-merged/{date}/` using one HF API call (or local scan) and embeds it into the training payload.
- Replaces runtime HF API data resolution with CDN-only URLs in the training script template.
- Adds deterministic sibling-repo selector for uploads (hash-slug mod N).
- Exposes a small verification step that checks CDN HEAD availability for listed files before training launch.

Scope:
- `/opt/axentx/vanguard/src/frontend/utils/surrogateManifest.ts` (new)
- `/opt/axentx/vanguard/src/frontend/templates/train.py.ejs` (or existing train.py) — update data loader to use CDN URLs + embedded manifest.
- `/opt/axentx/vanguard/src/frontend/utils/upload.ts` — add sibling-repo selector.

## 3. Implementation

### 3.1 Create manifest utility

```ts
// /opt/axentx/vanguard/src/frontend/utils/surrogateManifest.ts
import fs from 'fs/promises';
import path from 'path';
import crypto from 'crypto';

const HF_DATASET_OWNER = 'axentx';
const HF_DATASET_NAME = 'surrogate-1';
const SIBLING_COUNT = 5;

export interface FileEntry {
  cdn_url: string;    // https://huggingface.co/datasets/.../resolve/main/...
  repo: string;       // sibling repo name (for commit-cap spread)
  size: number;       // bytes (optional, from tree)
  hash: string;       // sha256 of path+size (content-addressable)
}

export interface SurrogateManifest {
  date: string;       // YYYY-MM-DD
  folder: string;     // batches/mirror-merged/{date}
  created_at: string; // ISO
  files: FileEntry[];
  total_files: number;
  total_bytes: number;
}

/**
 * Build manifest for a single date folder.
 * Prefer using a locally mirrored tree JSON if available to avoid HF API.
 * Fallback: call HF list_repo_tree (1 call, non-recursive) per subfolder if needed.
 */
export async function buildManifestForDate(
  date: string,
  options?: { treeJsonPath?: string }
): Promise<SurrogateManifest> {
  const folder = `batches/mirror-merged/${date}`;
  const files: FileEntry[] = [];

  if (options?.treeJsonPath) {
    // Use local tree snapshot (preferred to avoid HF API)
    const raw = await fs.readFile(options.treeJsonPath, 'utf8');
    const tree = JSON.parse(raw);
    for (const node of tree || []) {
      if (!node.path || node.type !== 'file') continue;
      if (!node.path.startsWith(folder)) continue;
      const cdn_url = `https://huggingface.co/datasets/${HF_DATASET_OWNER}/${HF_DATASET_NAME}/resolve/main/${encodeURIComponent(node.path)}`;
      const repo = pickSiblingRepo(node.path, node.size || 0);
      const hash = contentHash(node.path, node.size || 0);
      files.push({ cdn_url, repo, size: node.size || 0, hash });
    }
  } else {
    // NOTE: This branch makes ONE API call if you list per immediate subpath.
    // In practice, prefer providing treeJsonPath to avoid runtime API calls.
    throw new Error('Provide treeJsonPath to avoid HF API calls at build time.');
  }

  const total_files = files.length;
  const total_bytes = files.reduce((s, f) => s + (f.size || 0), 0);

  const manifest: SurrogateManifest = {
    date,
    folder,
    created_at: new Date().toISOString(),
    files,
    total_files,
    total_bytes,
  };

  return manifest;
}

/**
 * Deterministic sibling repo selector to spread HF commit cap.
 */
export function pickSiblingRepo(filePath: string, size: number): string {
  const slug = `${HF_DATASET_OWNER}/${HF_DATASET_NAME}`;
  const input = `${slug}:${filePath}:${size}`;
  const hash = crypto.createHash('sha256').update(input).digest('hex');
  const idx = parseInt(hash.slice(0, 8), 16) % SIBLING_COUNT;
  // sibling naming convention: surrogate-1, surrogate-1-s1 ... surrogate-1-s4
  return idx === 0 ? 'surrogate-1' : `surrogate-1-s${idx}`;
}

/**
 * Content-addressable hash for a file entry (stable across runs).
 */
export function contentHash(filePath: string, size: number): string {
  return crypto.createHash('sha256').update(`${filePath}:${size}`).digest('hex');
}

/**
 * Write manifest to disk.
 */
export async function writeManifest(
  manifest: SurrogateManifest,
  outDir: string
): Promise<string> {
  await fs.mkdir(outDir, { recursive: true });
  const outPath = path.join(outDir, `file-list-${manifest.date}.json`);
  await fs.writeFile(outPath, JSON.stringify(manifest, null, 2), 'utf8');
  return outPath;
}
```

### 3.2 Add CDN verification helper

```ts
// /opt/axentx/vanguard/src/frontend/utils/cdnVerify.ts
import fetch from 'node-fetch';

export async function verifyCdnAvailability(
  cdnUrl: string,
  timeoutMs = 5000
): Promise<{ ok: boolean; status?: number; error?: string }> {
  try {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(cdnUrl, { method: 'HEAD', signal: controller.signal });
    clearTimeout(id);
    return { ok: res.status === 200, status: res.status };
  } catch (err: any) {
    return { ok: false, error: err.message };
  }
}

export async function verifyManifestFiles(
  files: Array<{ cdn_url: string }>,
  concurrency = 20
): Promise<{ total: number; ok: number; failed: number; failures: string[] }> {
  const results = { total: files.length, ok: 0, failed: 0, failures: [] as string[] };
  const queue = [...files];
  const workers = Array.from({ length: concurrency }, async () => {
    while (queue.length) {
      const item = queue.shift();
      if (!item) break;
      const r = await verifyCdnAvailability(item.cdn_url);
      if (r.ok) results.ok++;
      else {
        results.failed++;
        results.failures.push(item.cdn_url);
      }
    }
  });
  await Promise.all(workers);
  return results;
}
```

### 3.3 Update training script template to use CDN + manifest

If you use a template (recommended), update it to embed the file list and use CDN URLs only:

```python
# /opt/axentx/vanguard/src/frontend/templates/train.py.ejs (or existing train.py)
import json
import os
import requests
from torch.utils.data import IterableDataset, DataLoader

MANIFEST_PATH = os.environ.get("SURROGATE_MANIFEST", "file-list-<%= date %>.json")

class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path=MANIFEST_PATH, buffer_size=1024*1024):
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
        self.url
