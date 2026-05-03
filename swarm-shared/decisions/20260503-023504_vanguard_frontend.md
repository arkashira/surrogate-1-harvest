# vanguard / frontend

# Final Synthesis: CDN-Bypass Dataset Manifest + Offline-First Preview

## Diagnosis (Consolidated)
- Frontend triggers authenticated Hugging Face API (`list_repo_tree`, `load_dataset`) on every preview/training launch → burns quota and risks 429s.
- No CDN bypass: data loads route through `/api/` instead of public `https://huggingface.co/datasets/.../resolve/main/...` URLs.
- No frontend manifest cache: each run re-enumerates repo files via API instead of embedding a static file list.
- No offline-first preview: UI cannot render dataset samples without a live API token/session and blocks on HF API latency.
- Missing deterministic repo→file mapping: training/frontend cannot pin exact file set for a given date/slug.

## Chosen Architecture
1. **Generate a static dataset manifest at build/admin time** (one authenticated API call) and embed it in the frontend bundle.
2. **Frontend uses CDN URLs exclusively** for previews (images, JSONL, parquet metadata) — no authenticated calls during runtime.
3. **Offline-first preview**: render cached manifest and fetch sample rows via CDN with abort + row limits.
4. **Deterministic pinning**: manifest includes `fileListSha256` and `generatedAt`; CI can produce dated manifests for reproducibility.

## Implementation

### 1) Create `/opt/axentx/vanguard/src/frontend/lib/dataset-cache.ts`
```ts
// /opt/axentx/vanguard/src/frontend/lib/dataset-cache.ts

export interface DatasetFile {
  path: string;
  cdnUrl: string;
  size?: number;
}

export interface DatasetManifest {
  repo: string;           // e.g. "datasets/username/repo"
  folder: string;         // e.g. "batches/mirror-merged/2026-05-03"
  files: DatasetFile[];
  cdnRoot: string;        // https://huggingface.co/datasets/.../resolve/main
  generatedAt: string;    // ISO timestamp
  fileListSha256?: string;
}

const CDN_ROOT = (repo: string) =>
  `https://huggingface.co/datasets/${repo}/resolve/main`;

export function buildDatasetManifest(
  repo: string,
  folder: string,
  filePaths: string[]
): DatasetManifest {
  const files = filePaths.map((path) => ({
    path,
    cdnUrl: `${CDN_ROOT(repo)}/${folder}/${encodeURIComponent(path)}`,
  }));
  return {
    repo,
    folder,
    files,
    cdnRoot: CDN_ROOT(repo),
    generatedAt: new Date().toISOString(),
  };
}

export async function fetchCdnText(url: string, signal?: AbortSignal): Promise<string> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${res.statusText}`);
  return res.text();
}

export async function* readJsonlPreview(
  url: string,
  maxLines = 50,
  signal?: AbortSignal
) {
  const text = await fetchCdnText(url, signal);
  const lines = text.split(/\r?\n/).filter(Boolean);
  for (let i = 0; i < Math.min(maxLines, lines.length); i++) {
    try {
      yield JSON.parse(lines[i]);
    } catch {
      // skip malformed
    }
  }
}
```

### 2) Create `/opt/axentx/vanguard/src/frontend/components/DatasetPreview.tsx`
```tsx
// /opt/axentx/vanguard/src/frontend/components/DatasetPreview.tsx
import { useEffect, useState } from "react";
import { DatasetManifest, readJsonlPreview } from "../lib/dataset-cache";

interface Props {
  manifest: DatasetManifest;
}

export default function DatasetPreview({ manifest }: Props) {
  const [previewRows, setPreviewRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    const firstJsonl = manifest.files.find((f) => f.path.endsWith(".jsonl"));
    if (!firstJsonl) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    (async () => {
      const rows: any[] = [];
      for await (const row of readJsonlPreview(firstJsonl.cdnUrl, 20, controller.signal)) {
        rows.push(row);
      }
      if (!controller.signal.aborted) setPreviewRows(rows);
    })().finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });

    return () => controller.abort();
  }, [manifest]);

  return (
    <section className="dataset-preview">
      <h3>{manifest.folder}</h3>
      <p className="muted">
        {manifest.files.length} files · manifest generated {new Date(manifest.generatedAt).toLocaleString()}
      </p>

      {loading && <p>Loading preview...</p>}
      {!loading && previewRows.length === 0 && <p>No JSONL preview available.</p>}

      <div className="preview-table">
        {previewRows.map((row, idx) => (
          <pre key={idx} className="code-block">
            {JSON.stringify(row, null, 2)}
          </pre>
        ))}
      </div>

      <ul className="file-list">
        {manifest.files.slice(0, 20).map((f) => (
          <li key={f.path}>
            <a href={f.cdnUrl} target="_blank" rel="noopener noreferrer">
              {f.path}
            </a>
          </li>
        ))}
        {manifest.files.length > 20 && (
          <li className="muted">+{manifest.files.length - 20} more</li>
        )}
      </ul>
    </section>
  );
}
```

### 3) Admin script to generate manifest (run on Mac)
```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/scripts/gen-manifest.sh
set -euo pipefail

REPO="datasets/username/repo"
FOLDER="batches/mirror-merged/2026-05-03"
OUT="/opt/axentx/vanguard/src/frontend/data/manifest-latest.json"

# One authenticated API call (run after rate-limit window)
# Requires HF_TOKEN in env.
node -e "
import { HfApi } from 'huggingface-hub';
const api = new HfApi();
const tree = await api.listRepoTree('$REPO', '$FOLDER', { recursive: false });
const files = tree.filter(t => t.type === 'file').map(t => t.path);
const manifest = {
  repo: '$REPO',
  folder: '$FOLDER',
  files: files.map(p => ({ path: p, cdnUrl: \`https://huggingface.co/datasets/$REPO/resolve/main/$FOLDER/\${encodeURIComponent(p)}\` })),
  cdnRoot: \`https://huggingface.co/datasets/$REPO/resolve/main\`,
  generatedAt: new Date().toISOString()
};
import { writeFileSync } from 'fs';
writeFileSync('$OUT', JSON.stringify(manifest, null, 2));
console.log('Manifest written to $OUT');
"
```

Add to `package.json`:
```json
"gen:manifest": "bash scripts/gen-manifest.sh"
```

### 4) Wire into app entry
```tsx
// /opt/axentx/vanguard/src/frontend/App.tsx
import manifest from "./data/manifest-latest.json";
import DatasetPreview from "./components/DatasetPreview";

export default function App() {
  return (
    <main>
      <h1>Vanguard — Dataset Preview</h1>
      <DatasetPreview manifest={manifest} />
    </main>
  );
}
```

## Verification Steps
1. Run `npm run gen:manifest` (or `bash scripts/gen-manifest.sh`) with `HF_TOKEN` set.
2. Confirm `src/frontend/data/manifest-latest.json` is created and contains correct CDN URLs.
3. Start the frontend dev server and verify:
   - Dataset preview renders without authenticated HF API calls.
  
