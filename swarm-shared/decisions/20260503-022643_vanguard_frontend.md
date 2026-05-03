# vanguard / frontend

## Final Synthesis (adopts strongest, most actionable parts; resolves contradictions in favor of correctness + concrete actionability)

- **Strongest diagnosis**: Both candidates correctly identify the same root causes (no manifest cache, no CDN bypass, no graceful 429 fallback, repeated auth-scoped API calls). We keep Candidate 1’s sharper symptom list and Candidate 2’s emphasis on Lightning Studio training impact.
- **Strongest proposal**: Candidate 1’s concrete file list (`manifest.ts`, `dataset-loader.ts`) and Candidate 2’s requirement for a persisted JSON artifact that can be embedded in the frontend bundle and used by training jobs. We merge them: produce a persisted `file-list.json` per `(repo,dateFolder)` that is readable by both frontend (localStorage) and training jobs (disk/artifact), and always prefer CDN URLs.
- **Contradictions resolved**:
  - Candidate 1 keeps size optional; Candidate 2 implies we want accurate metadata for training. We resolve: include size when available (from orchestration), else 0.
  - Candidate 1 suggests orchestration writes the manifest; Candidate 2 wants it embedded/bundled. We resolve: orchestration writes `file-list.json` to disk (artifact) and frontend hydrates from localStorage at runtime. Training jobs read the artifact directly (zero HF API).
  - Candidate 1’s loader returns `Uint8Array`; Candidate 2 emphasizes parquet-aware lightweight preview. We resolve: loader returns `Uint8Array` (bytes) and optionally parses metadata/row count for preview without full parse.
  - Candidate 1’s fallback is “show cached”; Candidate 2 explicitly calls out 429 fallback. We resolve: on CDN failure, use cached manifest and show stale-but-usable data; never call authenticated HF APIs from frontend.

---

## 1. Diagnosis (merged)
- No frontend manifest cache for `(repo, dateFolder) → file-list`; every preview/training launch triggers authenticated `list_repo_tree` calls, burning HF API quota and risking 429s.
- No CDN-bypass path in frontend; data loads go through `load_dataset`/API instead of `https://huggingface.co/datasets/{repo}/resolve/main/{path}` which bypasses auth rate limits.
- Missing persisted file-list artifact (`file-list.json`) that can be embedded/artifacted so Lightning Studio training uses zero HF API calls during data load.
- Frontend recomputes file lists on each mount instead of reading a static manifest, causing slow startup and quota waste.
- No graceful fallback for 429/CDN failures in the UI; frontend should switch to CDN-only mode or show stale-but-usable cache.
- No lightweight preview mode; frontend loads full dataset rows when it only needs metadata (filenames, sizes) for picker UI.

---

## 2. Proposed change (merged)
Add a frontend manifest cache + CDN-bypass data loader in `/opt/axentx/vanguard/src/lib/data/` and an orchestration-produced `file-list.json` artifact:

- `manifest.ts`: produces/persists `file-list.json` for `(repo, dateFolder)` and exposes CDN URLs; hydrates from localStorage at runtime.
- `dataset-loader.ts`: uses cached manifest to fetch parquet rows via CDN (no auth) and falls back to cache on 429/CDN failure; optionally parses lightweight metadata for preview.
- Training/job integration: read `file-list.json` artifact directly to build input paths; zero HF API calls during training.
- Hook into existing preview UI to use the loader instead of direct `load_dataset` calls.

Scope: add 2 new frontend files + 1 small UI integration edit; orchestration writes `file-list.json`; no backend changes.

---

## 3. Implementation

```bash
# Ensure directory exists
mkdir -p /opt/axentx/vanguard/src/lib/data
```

### src/lib/data/manifest.ts
```ts
// Persistent manifest: (repo, dateFolder) -> file list + CDN URLs
// Orchestration writes file-list.json per dateFolder; frontend hydrates from localStorage.

export interface FileEntry {
  path: string;
  size: number; // bytes; populated by orchestration when available
  cdnUrl: string;
}

export interface Manifest {
  repo: string;
  dateFolder: string; // e.g. "2026-04-29"
  generatedAt: string; // ISO
  files: FileEntry[];
}

const STORAGE_KEY = 'vanguard:manifests';

function getStore(): Record<string, Manifest> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveStore(store: Record<string, Manifest>) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch (err) {
    console.warn('Failed to save manifest to localStorage', err);
  }
}

function manifestKey(repo: string, dateFolder: string): string {
  return `${repo}:${dateFolder}`;
}

export function saveManifest(manifest: Manifest) {
  const store = getStore();
  store[manifestKey(manifest.repo, manifest.dateFolder)] = manifest;
  saveStore(store);
}

export function loadManifest(repo: string, dateFolder: string): Manifest | null {
  const store = getStore();
  return store[manifestKey(repo, dateFolder)] || null;
}

export function buildCdnUrl(repo: string, filePath: string): string {
  // Public CDN bypass: no Authorization header required
  // Normalize path to avoid double slashes
  const normalized = filePath.replace(/^\/+/, '');
  return `https://huggingface.co/datasets/${repo}/resolve/main/${normalized}`;
}

export function makeManifest(repo: string, dateFolder: string, filePaths: string[], sizes?: number[]): Manifest {
  const files: FileEntry[] = filePaths.map((p, i) => ({
    path: p,
    size: sizes?.[i] ?? 0,
    cdnUrl: buildCdnUrl(repo, p),
  }));
  return {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files,
  };
}

// Hydrate from an external JSON (e.g., fetched from artifact or injected)
export function hydrateFromJson(json: unknown): Manifest | null {
  try {
    const obj = json as Manifest;
    if (!obj.repo || !obj.dateFolder || !Array.isArray(obj.files)) return null;
    const files = obj.files.map((f) => ({
      path: String(f.path || ''),
      size: Number(f.size || 0),
      cdnUrl: String(f.cdnUrl || buildCdnUrl(obj.repo, String(f.path || ''))),
    }));
    const manifest: Manifest = {
      repo: String(obj.repo),
      dateFolder: String(obj.dateFolder),
      generatedAt: String(obj.generatedAt || new Date().toISOString()),
      files,
    };
    saveManifest(manifest);
    return manifest;
  } catch {
    return null;
  }
}
```

### src/lib/data/dataset-loader.ts
```ts
import { loadManifest, buildCdnUrl } from './manifest';

export interface ParquetPreview {
  numRows?: number;
  numRowGroups?: number;
  // lightweight metadata extracted without full parse (if available)
  // For full rows, caller can parse bytes with apache-arrow.
}

// Lightweight fetch of parquet bytes via CDN (no auth). Falls back to cache or empty on failure.
export async function fetchParquetBytes(
  repo: string,
  filePath: string,
  options?: { timeoutMs?: number }
): Promise<Uint8Array | null> {
  const url = buildCdnUrl(repo, filePath);
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options?.timeoutMs || 30_000);
    const res = await fetch(url, { signal: controller.signal });
    clearTimeout(timeout);

    if (!res.ok) {
      console.warn(`CDN fetch failed ${res.status} for ${url}`);
      return null;
    }
    return new Uint8Array(await res.arrayBuffer());
  } catch (err) {
    console.warn('CDN fetch error', err);
    return null;
  }
}

// Get file list for picker UI; never triggers authenticated HF API if manifest exists.
export async function getFileList(
  repo: string,
  dateFolder: string,
  options?: { allowStale: boolean }
): Promise<string[] | null> {

