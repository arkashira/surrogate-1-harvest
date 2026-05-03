# vanguard / frontend

## Final Synthesis (Best Parts + Correctness + Actionability)

### 1. Diagnosis (merged, corrected)
- **No content-addressed manifest** for dataset ingestion → non-reproducible file lists, 429 HF API rate-limits, flaky runs.
- **Mixed-schema files** from `dataset-mirror` land in `enriched/` without projection to `{prompt,response}` → bloated payloads, parsing errors, inconsistent UI shapes.
- **No CDN-bypass strategy** → frontend uses HF `/api/` endpoints for previews instead of public CDN URLs, exhausting auth rate limits.
- **No local cache/ETag layer** for repeated dataset browsing → every navigation re-fetches metadata, amplifying API calls and latency.
- **Missing deterministic repo selection** for HF writes → commits concentrate on one repo and hit the 128/hr cap.
- **No Lightning Studio reuse logic** in UI/orchestration → creating new studios per iteration burns quota and causes idle-stop training deaths.
- **UI does not surface deterrents/errors clearly** → users retry failed ingestion and compound rate limits.

### 2. Proposed Change (single, high-leverage)
Add a small frontend module and supporting orchestration that:
- Generates and embeds a **content-addressed file manifest** (one API call per folder) so training can run CDN-only.
- Projects `dataset-mirror` files to `{prompt,response}` before any upload/display.
- Uses **HF CDN URLs** for file previews (bypassing `/api/` auth).
- Adds a **local cache/ETag layer** for dataset browsing to reduce repeated metadata calls.
- Deterministically picks one of 5 sibling repos for writes via hash-slug to spread load.
- Reuses running Lightning Studio instances instead of recreating.
- Improves UI error/deterrent surfacing and retry/backoff behavior.

Scope:
- New: `/opt/axentx/vanguard/src/frontend/lib/hf-gateway.ts`
- Light edits: main upload/ingest form component (e.g., `IngestForm.tsx`)
- New: lightweight caching utility and orchestration script for manifest generation.

### 3. Implementation

#### 3.1 `/opt/axentx/vanguard/src/frontend/lib/hf-gateway.ts`

```ts
// src/frontend/lib/hf-gateway.ts
import { writeFileSync } from 'fs';
import { resolve as pathResolve } from 'path';

const HF_API = 'https://huggingface.co/api';
const HF_CDN = 'https://huggingface.co/datasets';
const HF_TOKEN = process.env.HF_TOKEN || '';

export interface FileEntry {
  path: string;
  size: number;
  type: 'file' | 'directory';
}

export interface Manifest {
  repo: string;         // e.g. 'datasets/myorg/surrogate-1'
  folder: string;       // e.g. 'batches/mirror-merged/2026-05-03'
  files: string[];      // relative paths within folder
  sha256: string;       // manifest content hash
  generatedAt: string;  // ISO
}

/**
 * List folder contents once (non-recursive) and save manifest.
 * Intended for orchestration layer (backend/CLI) before training.
 */
export async function generateManifest(
  repo: string,
  folder: string,
  outPath: string
): Promise<Manifest> {
  const url = `${HF_API}/repos/${repo}/tree/${encodeURIComponent(folder)}?recursive=false`;
  const resp = await fetch(url, {
    headers: HF_TOKEN ? { Authorization: `Bearer ${HF_TOKEN}` } : {},
  });
  if (!resp.ok) throw new Error(`HF tree failed: ${resp.status} ${await resp.text()}`);
  const tree: FileEntry[] = await resp.json();

  const files = tree
    .filter((f) => f.type === 'file')
    .map((f) => f.path.replace(new RegExp(`^${folder}/?`), ''))
    .filter(Boolean);

  const manifest: Manifest = {
    repo,
    folder,
    files,
    sha256: '',
    generatedAt: new Date().toISOString(),
  };

  const crypto = await import('crypto');
  const hash = crypto.createHash('sha256');
  hash.update(JSON.stringify(manifest, null, 0));
  manifest.sha256 = hash.digest('hex');

  writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  return manifest;
}

/**
 * Return public CDN URL for a dataset file (no auth required).
 */
export function cdnUrl(repo: string, filePath: string): string {
  return `${HF_CDN}/${repo}/resolve/main/${encodeURIComponent(filePath)}`;
}

/**
 * Deterministic sibling repo selector (spread writes across 5 repos).
 * repoBase example: 'datasets/myorg/surrogate-1'
 * returns one of:
 *  - datasets/myorg/surrogate-1
 *  - datasets/myorg/surrogate-1-sib1
 *  - ...
 */
export function pickSiblingRepo(repoBase: string, slug: string): string {
  const crypto = require('crypto');
  const idx = parseInt(crypto.createHash('sha256').update(slug).digest('hex').slice(0, 8), 16) % 5;
  if (idx === 0) return repoBase;
  return `${repoBase}-sib${idx}`;
}

/**
 * Lightweight projection for dataset-mirror rows to {prompt,response}.
 * Accepts raw JSON lines (or parsed objects) and returns projected pairs.
 */
export function projectMirrorRows(rawRows: any[]): Array<{ prompt: string; response: string }> {
  return rawRows.map((row) => {
    const prompt = row.prompt || row.input || row.question || row.text || '';
    const response = row.response || row.output || row.answer || row.completion || '';
    return { prompt: String(prompt), response: String(response) };
  });
}

/**
 * Lightweight fetch with ETag/local cache support for dataset metadata.
 * Uses a simple in-memory cache for this process; for persistence, use a file-based cache.
 */
const metadataCache = new Map<string, { etag?: string; data: any; ts: number }>();

export async function cachedFetch(
  url: string,
  options: RequestInit & { useCache?: boolean; cacheTtlMs?: number } = {}
): Promise<{ data: any; cached: boolean; etag?: string }> {
  const { useCache = true, cacheTtlMs = 5 * 60 * 1000, ...fetchOpts } = options;
  const cached = useCache ? metadataCache.get(url) : undefined;
  const headers: Record<string, string> = {};

  if (HF_TOKEN) headers['Authorization'] = `Bearer ${HF_TOKEN}`;
  if (cached && cached.etag) headers['If-None-Match'] = cached.etag;

  const resp = await fetch(url, { ...fetchOpts, headers });

  if (resp.status === 304 && cached) {
    // Not modified: return cached data
    return { data: cached.data, cached: true, etag: cached.etag };
  }

  if (!resp.ok) throw new Error(`Fetch failed: ${resp.status} ${await resp.text()}`);

  const etag = resp.headers.get('ETag') || undefined;
  const data = await resp.json();

  if (useCache) {
    metadataCache.set(url, { etag, data, ts: Date.now() });
  }

  return { data, cached: false, etag };
}

/**
 * Placeholder: integrate Lightning SDK in backend. Frontend calls an orchestration endpoint.
 * Returns null to signal orchestration layer to create/start one.
 */
export async function findRunningStudio(
  teamspace: string,
  studioName: string
): Promise<{ id: string; status: string; url?: string } | null> {
  return null;
}
```

#### 3.2 Lightweight caching utility (browser/node)

Create `/opt/axentx/vanguard/src/frontend/lib/cache.ts`:

```ts
// src/frontend/lib/cache.ts
type CacheEntry<T> = { value: T; expires: number; etag?: string };

export class SimpleCache<T = any> {
  private store = new Map<string, CacheEntry<T>>();

