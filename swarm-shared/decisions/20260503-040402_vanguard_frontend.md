# vanguard / frontend

## Final Synthesis (adopts strongest, most actionable parts; resolves contradictions)

- **Core diagnosis (shared)**  
  - Frontend must **never call HF API** (`list_repo_files`/`load_dataset`) at runtime — causes 429s and non-reproducible views.  
  - There is **no content-addressed manifest** in the UI → training jobs and UI can diverge.  
  - Mixed-schema files from `dataset-mirror/enriched/` cannot be rendered raw → downstream preview breaks.  
  - Frontend must use **CDN-only URLs** (`resolve/main/...`) and never hit `/api/` for file previews.

- **Chosen approach (combines best parts)**  
  - Ship a **small, deterministic manifest loader + CDN-only preview module** in the frontend.  
  - Manifest is **static, content-addressed, and generated offline** (ops pipeline) and served from `static/` (or CDN).  
  - Frontend **projects only `{prompt,response}`** for previews (schema-safe, bandwidth-efficient).  
  - Build is **environment-aware** via injected public envs (`PUBLIC_DATASET_REPO`, `PUBLIC_MANIFEST_PATH`).  
  - Keep scope minimal (~120–180 lines), framework-agnostic core, with framework-specific integration stubs.

---

## 1. Manifest contract (single source of truth)

Place generated manifest at:  
`static/manifests/{datasetRepo}/{date}/file-list.json`  
(or at repo root `manifest.json` if simpler).

Example `file-list.json`:
```json
{
  "repo": "org/vanguard-dataset",
  "dateFolder": "2026-05-03",
  "generatedAt": "2026-05-03T12:00:00Z",
  "files": [
    { "path": "batches/mirror-merged/2026-05-03/abc123.parquet", "sha256": "e3b0c442...", "bytes": 12345 },
    { "path": "previews/2026-05-03/abc123.jsonl", "sha256": "a1b2c3...", "bytes": 456 }
  ]
}
```

Notes:
- `sha256` enables content-addressed verification.  
- Include small preview files (JSONL) produced by ingestion for browser-safe previews; do **not** attempt to parse parquet in browser.

---

## 2. Frontend modules (TypeScript)

### `src/lib/datasetManifest.ts`
```ts
// Loads a static, content-addressed manifest produced by ingestion/ops.
export interface ManifestFile {
  path: string;
  sha256: string;
  bytes?: number;
}

export interface DatasetManifest {
  repo: string;
  dateFolder: string;
  generatedAt?: string;
  files: ManifestFile[];
}

export async function loadManifest(
  manifestPath: string = import.meta.env.PUBLIC_MANIFEST_PATH || '/manifest.json'
): Promise<DatasetManifest> {
  const res = await fetch(manifestPath, { cache: 'no-store' });
  if (!res.ok) {
    throw new Error(`Failed to load dataset manifest: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<DatasetManifest>;
}
```

### `src/lib/cdnPreview.ts`
```ts
import type { DatasetManifest } from './datasetManifest';

export function buildCdnUrl(repo: string, filePath: string): string {
  // CDN-only; no Authorization header; higher rate limits.
  // Ensure repo is URL-safe (org/repo).
  return `https://huggingface.co/datasets/${repo}/resolve/main/${filePath}`;
}

// Schema-safe projection for preview.
export function projectPromptResponse(record: any): { prompt: string; response: string } {
  return {
    prompt: String(record.prompt ?? record.input ?? record.question ?? ''),
    response: String(record.response ?? record.output ?? record.answer ?? ''),
  };
}

// Fetch a preview row from a small JSON/JSONL file produced by ingestion.
// Do NOT attempt to read parquet in the browser.
export async function fetchCdnPreview(
  repo: string,
  filePath: string,
  signal?: AbortSignal
): Promise<{ prompt: string; response: string } | null> {
  const url = buildCdnUrl(repo, filePath);
  const res = await fetch(url, { signal });
  if (!res.ok) return null;

  try {
    const text = await res.text();
    const lines = text.trim().split('\n').filter(Boolean);
    if (lines.length === 0) return null;

    // Try first line as JSON (JSONL produced by ingestion)
    const parsed = JSON.parse(lines[0]);
    return projectPromptResponse(parsed);
  } catch {
    return null;
  }
}

export function createFileList(manifest: DatasetManifest) {
  return manifest.files.map((f) => ({
    path: f.path,
    cdnUrl: buildCdnUrl(manifest.repo, f.path),
    sha256: f.sha256,
    bytes: f.bytes,
  }));
}
```

---

## 3. Framework integration (examples)

### SvelteKit: `src/routes/dataset/+page.ts`
```ts
import { loadManifest, type DatasetManifest } from '$lib/datasetManifest';
import { createFileList } from '$lib/cdnPreview';
import { PUBLIC_DATASET_REPO, PUBLIC_MANIFEST_PATH } from '$env/static/public';

export const ssr = true;

export async function load() {
  const manifest = await loadManifest(PUBLIC_MANIFEST_PATH);
  const files = createFileList(manifest);

  return {
    props: {
      repo: PUBLIC_DATASET_REPO || manifest.repo,
      files,
      manifestDate: manifest.dateFolder,
    },
  };
}
```

In `+page.svelte`, render `files` and call `fetchCdnPreview(repo, file.path)` for lightweight previews (client-side).

### React (Vite) example snippet
```tsx
import { useEffect, useState } from 'react';
import { loadManifest, type DatasetManifest } from './lib/datasetManifest';
import { createFileList, fetchCdnPreview } from './lib/cdnPreview';

export function DatasetView() {
  const [items, setItems] = useState<Array<{ path: string; cdnUrl: string; preview?: { prompt: string; response: string } }>>([]);

  useEffect(() => {
    (async () => {
      const manifest = await loadManifest(import.meta.env.PUBLIC_MANIFEST_PATH);
      const files = createFileList(manifest);
      setItems(files);

      // Optionally load previews for small preview files
      for (const f of files) {
        // Only try previews for likely small JSON/JSONL files
        if (f.path.endsWith('.jsonl') || f.path.endsWith('.json')) {
          const p = await fetchCdnPreview(manifest.repo, f.path);
          if (p) {
            setItems((prev) =>
              prev.map((x) => (x.path === f.path ? { ...x, preview: p } : x))
            );
          }
        }
      }
    })();
  }, []);

  return (
    <div>
      {items.map((it) => (
        <div key={it.path}>
          <a href={it.cdnUrl} target="_blank" rel="noreferrer">{it.path}</a>
          {it.preview && (
            <pre>{JSON.stringify(it.preview, null, 2)}</pre>
          )}
        </div>
      ))}
    </div>
  );
}
```

---

## 4. Build & environment (Vite)

`vite.config.ts`
```ts
import { defineConfig } from 'vite';

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(process.env.npm_package_version),
  },
});
```

`.env` (committed or injected in CI/CD)
```
PUBLIC_DATASET_REPO=org/vanguard-dataset
PUBLIC_MANIFEST_PATH=/manifests/org/vanguard-dataset
