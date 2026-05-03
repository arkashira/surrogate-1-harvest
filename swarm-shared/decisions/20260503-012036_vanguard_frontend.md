# vanguard / frontend

## 1. Diagnosis

- Frontend still triggers authenticated HF API calls (`list_repo_tree`, dataset metadata) on page load or training start, burning 1000/5min quota and risking 429s.
- No persisted `(repo, dateFolder)` manifest; every visit re-enumerates files via `/api/` endpoints instead of using CDN-only paths.
- Data loader uses authenticated `/api/` file URLs instead of public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`), missing the CDN bypass opportunity.
- No offline/retry resilience for HF API 429 or network failures; UX stalls or crashes.
- No visibility into remaining HF quota or last manifest freshness; devs/operators can’t tell why requests are slow or failing.

## 2. Proposed change

File: `/opt/axentx/vanguard/src/lib/hf-client.ts` (create if absent)  
File: `/opt/axentx/vanguard/src/routes/training/+page.ts` (or equivalent route loader)  
File: `/opt/axentx/vanguard/src/lib/data-loader.ts` (or equivalent)  
Scope: add a lightweight manifest service + CDN-only fetcher + quota guard; swap data loader to use public CDN URLs and require a pre-fetched manifest.

## 3. Implementation

```bash
# Ensure frontend structure exists
mkdir -p /opt/axentx/vanguard/src/lib /opt/axentx/vanguard/src/routes/training
```

`/opt/axentx/vanguard/src/lib/hf-client.ts`
```ts
// Lightweight HF client for frontend: manifest fetch + CDN-only downloads.
// Avoids authenticated /api/ calls during training/data-load.

const HF_API_BASE = 'https://huggingface.co/api';
const HF_CDN_BASE = 'https://huggingface.co/datasets';

export interface RepoFile {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

export interface RepoManifest {
  repo: string;           // e.g. 'username/dataset'
  folder: string;         // e.g. 'batches/mirror-merged/2026-04-29'
  files: string[];        // relative paths under folder
  generatedAt: number;    // unix ms
  ttlMs: number;          // default 1h
}

export class HFClient {
  private token?: string;
  private quotaRefillAfter: number | null = null;

  constructor(opts?: { token?: string }) {
    this.token = opts?.token;
  }

  // Fetch manifest ONCE per (repo,folder) and cache in localStorage.
  // Caller should run this from a route loader (server-side or during build/SSR)
  // or from an infrequent client init step.
  async getOrFetchManifest(
    repo: string,
    folder: string,
    opts?: { ttlMs?: number; skipCache?: boolean }
  ): Promise<RepoManifest> {
    const ttl = opts?.ttlMs ?? 60 * 60 * 1000;
    const cacheKey = `hf-manifest:${repo}:${folder}`;
    const cached = !opts?.skipCache ? localStorage.getItem(cacheKey) : null;

    if (cached) {
      try {
        const m: RepoManifest = JSON.parse(cached);
        if (Date.now() - m.generatedAt < m.ttlMs) return m;
      } catch {
        localStorage.removeItem(cacheKey);
      }
    }

    // Single authenticated call to list folder (non-recursive).
    // If 429, throw so caller can retry after waiting.
    const url = `${HF_API_BASE}/repos/datasets/${repo}/tree?path=${encodeURIComponent(
      folder
    )}&recursive=false`;
    const res = await fetch(url, {
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
    });

    if (res.status === 429) {
      // Suggest wait 360s per pattern
      this.quotaRefillAfter = Date.now() + 360_000;
      throw new Error('HF API rate limit 429 — wait 360s before retry');
    }

    if (!res.ok) {
      throw new Error(`HF API error ${res.status}: ${await res.text()}`);
    }

    const tree: RepoFile[] = await res.json();
    const files = tree.filter((t) => t.type === 'file').map((t) => t.path);

    const manifest: RepoManifest = {
      repo,
      folder,
      files,
      generatedAt: Date.now(),
      ttlMs: ttl,
    };
    localStorage.setItem(cacheKey, JSON.stringify(manifest));
    return manifest;
  }

  // Return public CDN URL for a dataset file (no auth).
  cdnUrl(repo: string, filePath: string): string {
    return `${HF_CDN_BASE}/${repo}/resolve/main/${filePath}`;
  }

  // Fetch file via CDN (no Authorization header).
  async fetchCdn(repo: string, filePath: string, init?: RequestInit): Promise<Response> {
    const url = this.cdnUrl(repo, filePath);
    // Important: do NOT send Authorization header to CDN path.
    const { headers: _, ...safeInit } = init ?? {};
    return fetch(url, safeInit);
  }

  // Convenience: fetch and parse NDJSON lines (common for surrogate-1 pairs)
  async fetchCdnNdjson<T = { prompt: string; response: string }>(
    repo: string,
    filePath: string,
    signal?: AbortSignal
  ): Promise<T[]> {
    const res = await this.fetchCdn(repo, filePath, { signal });
    if (!res.ok) throw new Error(`CDN fetch failed ${res.status}`);
    const text = await res.text();
    return text
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => JSON.parse(l));
  }

  // Return seconds until suggested retry after 429 (if known)
  get retryAfterSeconds(): number | null {
    if (!this.quotaRefillAfter) return null;
    const s = Math.ceil((this.quotaRefillAfter - Date.now()) / 1000);
    return s > 0 ? s : null;
  }
}
```

`/opt/axentx/vanguard/src/lib/data-loader.ts`
```ts
import { HFClient } from './hf-client';

const HF_REPO = 'username/dataset'; // default; override per training run
const HF_FOLDER = 'batches/mirror-merged'; // date subfolders live here

const client = new HFClient({ token: import.meta.env.VITE_HF_TOKEN });

export interface TrainingBatch {
  prompts: string[];
  responses: string[];
  sourceFile: string;
}

// Load one date-folder manifest and stream files via CDN.
// Designed to be called from route loader or training launcher (not on every render).
export async function loadDateFolder(
  dateFolder: string,
  opts?: { signal?: AbortSignal }
): Promise<TrainingBatch[]> {
  const folder = `${HF_FOLDER}/${dateFolder}`;
  const manifest = await client.getOrFetchManifest(HF_REPO, folder, { ttlMs: 60 * 60 * 1000 });

  const out: TrainingBatch[] = [];
  for (const file of manifest.files) {
    if (opts?.signal?.aborted) break;
    try {
      const rows = await client.fetchCdnNdjson(HF_REPO, file, opts?.signal);
      out.push({
        prompts: rows.map((r) => r.prompt ?? ''),
        responses: rows.map((r) => r.response ?? ''),
        sourceFile: file,
      });
    } catch (err) {
      console.warn(`Failed to load ${file}`, err);
      // continue with other files
    }
  }
  return out;
}
```

`/opt/axentx/vanguard/src/routes/training/+page.ts` (example for SvelteKit; adapt to your framework)
```ts
import { loadDateFolder } from '$lib/data-loader';
import type { PageLoad } = './$types';

export const load: PageLoad = async ({ fetch, depends }) => {
  // Declare dependency so framework knows this depends on data.
  depends
