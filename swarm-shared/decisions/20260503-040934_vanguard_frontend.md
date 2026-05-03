# vanguard / frontend

## Final synthesized solution (best parts, no contradictions, concrete + correct)

**Core diagnosis (accepted from both candidates)**
- Runtime HF API calls (`list_repo_tree`, `load_dataset`) cause 429s and non-reproducible views.
- No content-addressed manifest → frontend can’t pin file sets per snapshot.
- Mixed-schema `enriched/` parquet risks schema errors if code tries to coerce types.
- Frontend fetches via `/api/` (auth + rate-limit) instead of CDN-only URLs.
- No deterministic snapshot UI or pinned file listing.

**Chosen strategy (resolve contradictions in favor of correctness + actionability)**
- Use a **content-addressed manifest** checked into the repo (or served from CDN) so the frontend never calls HF listing APIs.
- Frontend **only** uses CDN URLs (`resolve/main/...`) — no Authorization header, no `/api/` file listing.
- Provide a **snapshot panel** that reads the manifest and exposes deterministic file links and metadata.
- Keep manifest format explicit (include `sha256` for integrity) and make CDN construction centralized and typed.
- Provide local dev fallback and clear verification steps.

---

### 1) Manifest (commit to repo; ops publishes updates)

Path (repo root or mirrored in CDN):
- `public/datasets/vanguard/manifest.json` (dev)
- In production: served at CDN `https://huggingface.co/datasets/<repo>/resolve/main/manifest.json`

```json
{
  "version": "1",
  "created": "2026-05-03T04:10:00Z",
  "repo": "vanguard",
  "snapshot": "2026-05-03",
  "files": [
    {
      "path": "enriched/2026-05-03/batch-001.parquet",
      "size": 204800,
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    },
    {
      "path": "enriched/2026-05-03/batch-002.parquet",
      "size": 198656,
      "sha256": "a7d1c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b856"
    }
  ]
}
```

Notes:
- `sha256` is required for integrity checks (optional at runtime but strongly recommended).
- `size` enables quick UI estimates.
- `snapshot` is human-readable date used for deterministic selection.

---

### 2) CDN helper (single source of truth)

`src/lib/cdn.ts`

```ts
export const HF_DATASETS_CDN = 'https://huggingface.co/datasets';

export interface ManifestFile {
  path: string;
  size: number;
  sha256?: string;
}

export interface Manifest {
  version: string;
  created: string;
  repo: string;
  snapshot: string;
  files: ManifestFile[];
}

export function cdnResolve(repo: string, path: string): string {
  // repo can be "vanguard" or "org/vanguard"
  return `${HF_DATASETS_CDN}/${repo}/resolve/main/${path}`;
}

export function cdnManifestUrl(repo: string, manifestPath = 'manifest.json'): string {
  return cdnResolve(repo, manifestPath);
}

export async function fetchManifest(repo: string, manifestPath = 'manifest.json'): Promise<Manifest | null> {
  try {
    const url = cdnManifestUrl(repo, manifestPath);
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    const json = (await res.json()) as unknown;
    // Minimal runtime validation (non-exhaustive)
    if (
      json &&
      typeof json === 'object' &&
      'version' in json &&
      'files' in json &&
      Array.isArray((json as any).files)
    ) {
      return json as Manifest;
    }
    return null;
  } catch {
    return null;
  }
}
```

---

### 3) Snapshot panel (deterministic, CDN-only)

`src/components/DatasetSnapshotPanel.tsx`

```tsx
import { useEffect, useState } from 'react';
import { fetchManifest, cdnResolve, Manifest } from '../lib/cdn';

interface Props {
  repo?: string;
  showFileHash?: boolean;
}

export default function DatasetSnapshotPanel({ repo = 'vanguard', showFileHash = false }: Props) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchManifest(repo)
      .then((m) => {
        if (!mounted) return;
        if (m) setManifest(m);
        else setError('Manifest not found or invalid');
      })
      .catch((e) => {
        if (!mounted) return;
        setError(String(e));
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [repo]);

  if (loading) return <div className="p-3 text-sm text-gray-500">Loading snapshot...</div>;
  if (error) return <div className="p-3 text-sm text-red-600">{error}</div>;
  if (!manifest) return null;

  return (
    <div className="border rounded p-3 bg-gray-50">
      <h3 className="font-semibold text-sm mb-1">Dataset Snapshot: {manifest.snapshot}</h3>
      <p className="text-xs text-gray-500 mb-2">Created: {manifest.created}</p>
      <p className="text-xs text-gray-500 mb-3">
        Files: {manifest.files.length} &nbsp;|&nbsp; Total size:{' '}
        {(manifest.files.reduce((s, f) => s + f.size, 0) / 1024).toFixed(1)} KB
      </p>

      <ul className="text-xs space-y-1 max-h-60 overflow-auto border-t pt-2">
        {manifest.files.map((f) => (
          <li key={f.path} className="flex items-center gap-2">
            <a
              href={cdnResolve(repo, f.path)}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline truncate"
              title={f.path}
            >
              {f.path}
            </a>
            <span className="text-gray-400 flex-shrink-0">{(f.size / 1024).toFixed(1)} KB</span>
            {showFileHash && f.sha256 && (
              <code className="text-gray-400 text-[10px] truncate" title={f.sha256}>
                {f.sha256.slice(0, 12)}…
              </code>
            )}
          </li>
        ))}
      </ul>

      <div className="mt-2 text-xs text-gray-400">
        All links use HuggingFace CDN (no API/auth). Manifest is content-addressed.
      </div>
    </div>
  );
}
```

---

### 4) Integrate into app

`src/App.tsx` (or main layout)

```tsx
import DatasetSnapshotPanel from './components/DatasetSnapshotPanel';

export default function App() {
  return (
    <div className="min-h-screen">
      {/* existing header/nav */}
      <main className="p-6">
        <div className="max-w-4
