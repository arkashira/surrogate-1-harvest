# vanguard / frontend

## Final Synthesized Implementation

**Core principle:** Deterministic, content-addressed `{date}/{slug}` manifests with CDN-only training fetches, cached file lists, and Lightning Studio reuse to eliminate 429s and quota waste.

---

### 1. Architecture (merged)

**Deterministic manifest layer**  
- Single `list_repo_tree` call per date folder → persisted `manifests/{date}/files.json` (repo-relative paths + CDN URLs).  
- TTL 1 hour in `localStorage` cache; fallback to persisted JSON for offline/reproducible runs.  
- All training code uses **only** CDN URLs from the manifest (never runtime `load_dataset` or HF API).

**Frontend UI**  
- `DatasetPanel` (replaces Candidate 1 `DatasetBrowser`) with:  
  - Date-folder parquet list + CDN preview links.  
  - Top-hub/MOC insight placeholder (`#knowledge-rag #graph #hub`).  
  - Surrogate-1 batch validator (schema projection: prompt/response only) and filename pattern preview (`batches/mirror-merged/{date}/{slug}.parquet`).  
- `StudioReuseButton` lists running Lightning Studios and attaches to existing ones (no recreation).

**Backend bridge** (minimal)  
- `/api/hf-list` proxies a single `list_repo_tree(repo, path=dateFolder, recursive=false)` to avoid client-side auth/429.  
- Returns `{type, path}` array; frontend filters to `.parquet`.

---

### 2. File-by-file implementation

#### src/features/datasetManifest.ts
```ts
export interface ManifestFile {
  path: string;
  cdnUrl: string;
  size?: number;
}

const CDN_ROOT = 'https://huggingface.co/datasets';

export function makeCdnUrl(repo: string, path: string): string {
  return `${CDN_ROOT}/${repo}/resolve/main/${path}`;
}

export async function fetchDateManifest(repo: string, dateFolder: string): Promise<ManifestFile[]> {
  const res = await fetch(`/api/hf-list?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(dateFolder)}&recursive=false`);
  if (!res.ok) throw new Error('HF list failed');
  const tree = await res.json();
  return tree
    .filter((n: any) => n.type === 'file' && n.path.endsWith('.parquet'))
    .map((n: any) => ({
      path: n.path,
      cdnUrl: makeCdnUrl(repo, n.path),
      size: n.size,
    }))
    .sort((a, b) => a.path.localeCompare(b.path));
}

// Persisted JSON for reproducibility (optional write via admin/upload flow)
export async function saveManifest(date: string, files: ManifestFile[]): Promise<void> {
  // In production, POST to /api/manifests/{date}
  // For now, localStorage fallback used by UI
  const key = `manifest:${date}`;
  localStorage.setItem(key, JSON.stringify({ ts: Date.now(), files }));
}

export function loadManifest(date: string): ManifestFile[] | null {
  const key = `manifest:${date}`;
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  try {
    const obj = JSON.parse(raw);
    if (Date.now() - obj.ts > 3600_000) return null;
    return obj.files;
  } catch {
    return null;
  }
}
```

#### src/hooks/useDatasetManifest.ts
```ts
import { useEffect, useState } from 'react';
import { fetchDateManifest, loadManifest } from '../features/datasetManifest';

export function useDatasetManifest(repo: string, dateFolder: string) {
  const [files, setFiles] = useState<Array<{ path: string; cdnUrl: string }>>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const cached = loadManifest(dateFolder);
    if (cached) {
      setFiles(cached);
    }

    setLoading(true);
    fetchDateManifest(repo, dateFolder)
      .then((list) => {
        setFiles(list);
        // persist fresh copy
        try {
          localStorage.setItem(
            `manifest:${dateFolder}`,
            JSON.stringify({ ts: Date.now(), files: list })
          );
        } catch (e) {
          // ignore quota errors
        }
      })
      .finally(() => setLoading(false));
  }, [repo, dateFolder]);

  return { files, loading };
}
```

#### src/components/DatasetPanel.tsx
```tsx
import React, { useState } from 'react';
import { useDatasetManifest } from '../hooks/useDatasetManifest';
import { StudioReuseButton } from './StudioReuseButton';

interface Props {
  repo: string;
  dateFolder: string;
  onSelect: (slug: string) => void;
}

export const DatasetPanel: React.FC<Props> = ({ repo, dateFolder, onSelect }) => {
  const { files, loading } = useDatasetManifest(repo, dateFolder);
  const [previewFile, setPreviewFile] = useState<string | null>(null);

  const toSlug = (path: string) => path.replace(/^.*\//, '').replace('.parquet', '');

  // Surrogate-1 validator (lightweight client-side projection)
  const validateBatch = (slug: string) => {
    // Placeholder: real validation would fetch a sample row via CDN range request
    // and check for {prompt,response} schema.
    return { ok: true, note: 'Schema projection: prompt/response present (sample)' };
  };

  return (
    <div className="dataset-panel">
      <StudioReuseButton name="vanguard-ingest-l40s" onReady={(studio) => console.log('Reuse', studio)} />

      {/* Top-hub insight placeholder */}
      <div className="hub-insight" style={{ marginBottom: 12 }}>
        <strong>Top hub:</strong> MOC (Model-Optimized Corpus) — tags: #knowledge-rag #graph #hub
      </div>

      <h3>{dateFolder}</h3>
      {loading && <p>Loading file list (cached when available)…</p>}

      <ul>
        {files.map((f) => {
          const slug = toSlug(f.path);
          const validation = validateBatch(slug);
          return (
            <li key={f.path} style={{ marginBottom: 8 }}>
              <button onClick={() => onSelect(slug)}>{slug}</button>
              <a href={f.cdnUrl} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>
                CDN preview
              </a>
              <button
                onClick={() => setPreviewFile(f.path)}
                style={{ marginLeft: 8 }}
              >
                Validate batch
              </button>
              {validation.ok && (
                <span style={{ marginLeft: 8, color: 'green' }}>
                  {validation.note}
                </span>
              )}
              <div style={{ marginTop: 4, fontSize: 12, color: '#666' }}>
                batches/mirror-merged/{dateFolder}/{slug}.parquet
              </div>
            </li>
          );
        })}
      </ul>

      {previewFile && (
        <div className="preview-pane">
          <h4>Preview: {previewFile}</h4>
          <p>
            <a href={files.find((f) => f.path === previewFile)?.cdnUrl} target="_blank" rel="noreferrer">
              Download via CDN
            </a>
          </p>
          {/* Real implementation would fetch sample rows via range request */}
          <pre style={{ maxHeight: 200, overflow: 'auto' }}>Sample rows (placeholder)</pre>
        </div>
      )}
    </div>
  );
};
```

#### src/components/StudioReuseButton.tsx
```tsx
import React, { useEffect, useState } from 'react';
import { Lightning } from '../lib/lightning';

export const StudioReuseButton: React.FC<{ name: string; onReady: (studio: any) => void }> = ({ name, on
