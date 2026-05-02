# airship / frontend

## Analysis & Highest-Value Increment

The surrogate-1 training pipeline is blocked by HF API rate limits during dataset ingestion. The highest-value <2h fix is implementing **CDN-only file manifest generation** for the surrogate training pipeline — this bypasses HF API auth limits entirely and enables Lightning Studio training with zero API calls during data load.

## Implementation Plan — CDN Manifest Generator (frontend)

**Scope**: ≤2h  
**Goal**: Add frontend utility to generate CDN-only file manifests for surrogate training repos, plus a simple UI to trigger/display manifests.

### 1. Create `src/lib/cdnManifest.ts`

```typescript
// src/lib/cdnManifest.ts
export interface CDNFile {
  path: string;
  url: string;
  size?: number;
}

export interface Manifest {
  repo_id: string;
  date_folder: string;
  generated_at: string;
  files: CDNFile[];
  total_files: number;
  total_size: number;
}

const HF_CDN_BASE = 'https://huggingface.co';

export async function generateCDNManifest(
  repo_id: string,
  date_folder: string,
  signal?: AbortSignal
): Promise<Manifest> {
  // Use tree endpoint (recursive=false) to list top-level in date folder
  // This is a single API call, then we construct CDN URLs
  const treeUrl = `https://huggingface.co/api/datasets/${repo_id}/tree/${encodeURIComponent(
    date_folder
  )}?recursive=false`;

  const res = await fetch(treeUrl, { signal });
  if (!res.ok) {
    throw new Error(`HF tree API failed: ${res.status}`);
  }

  const tree = await res.json();
  const files: CDNFile[] = [];
  let totalSize = 0;

  for (const node of tree) {
    if (node.type === 'file') {
      const cdnUrl = `${HF_CDN_BASE}/datasets/${repo_id}/resolve/main/${encodeURIComponent(
        date_folder
      )}/${encodeURIComponent(node.path)}`;
      files.push({
        path: `${date_folder}/${node.path}`,
        url: cdnUrl,
        size: node.size,
      });
      totalSize += node.size || 0;
    }
  }

  return {
    repo_id,
    date_folder,
    generated_at: new Date().toISOString(),
    files,
    total_files: files.length,
    total_size: totalSize,
  };
}

export function downloadManifest(manifest: Manifest, filename?: string) {
  const blob = new Blob([JSON.stringify(manifest, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || `manifest-${manifest.repo_id.split('/')[1]}-${manifest.date_folder}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
```

### 2. Add `src/components/CDNManifestGenerator.tsx`

```tsx
// src/components/CDNManifestGenerator.tsx
import { useState } from 'react';
import { generateCDNManifest, type Manifest, downloadManifest } from '../lib/cdnManifest';

export function CDNManifestGenerator() {
  const [repoId, setRepoId] = useState('axentx/surrogate-datasets');
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [loading, setLoading] = useState(false);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setLoading(true);
    setError(null);
    try {
      const m = await generateCDNManifest(repoId, dateFolder);
      setManifest(m);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto p-6 bg-white rounded-lg shadow">
      <h2 className="text-xl font-semibold mb-4">CDN Manifest Generator</h2>
      <p className="text-sm text-gray-600 mb-4">
        Generate CDN-only file manifests for surrogate training repos. Bypasses HF API rate limits
        by using public CDN URLs.
      </p>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Repo ID</label>
          <input
            type="text"
            value={repoId}
            onChange={(e) => setRepoId(e.target.value)}
            className="w-full px-3 py-2 border rounded"
            placeholder="owner/repo"
          />
        </div>

        <div>
          <label className="block text-sm font-medium mb-1">Date Folder</label>
          <input
            type="text"
            value={dateFolder}
            onChange={(e) => setDateFolder(e.target.value)}
            className="w-full px-3 py-2 border rounded"
            placeholder="YYYY-MM-DD"
          />
        </div>

        <button
          onClick={handleGenerate}
          disabled={loading}
          className="w-full bg-blue-600 text-white py-2 rounded hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? 'Generating...' : 'Generate Manifest'}
        </button>

        {error && <div className="text-red-600 text-sm">{error}</div>}

        {manifest && (
          <div className="mt-4 p-4 bg-gray-50 rounded">
            <div className="flex justify-between items-start mb-2">
              <div>
                <h3 className="font-medium">Manifest Generated</h3>
                <p className="text-xs text-gray-500">{manifest.generated_at}</p>
              </div>
              <button
                onClick={() => downloadManifest(manifest)}
                className="text-sm text-blue-600 hover:underline"
              >
                Download JSON
              </button>
            </div>
            <div className="grid grid-cols-3 gap-4 text-sm mb-3">
              <div>Files: {manifest.total_files}</div>
              <div>Size: {(manifest.total_size / 1024 / 1024).toFixed(2)} MB</div>
              <div>Repo: {manifest.repo_id}</div>
            </div>
            <div className="max-h-48 overflow-auto border rounded p-2">
              {manifest.files.slice(0, 10).map((f, i) => (
                <div key={i} className="text-xs truncate">
                  {f.path}
                </div>
              ))}
              {manifest.files.length > 10 && (
                <div className="text-xs text-gray-500 text-center py-1">
                  ... and {manifest.files.length - 10} more
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
```

### 3. Add route in `src/App.tsx`

```tsx
// Add to existing routes
import { CDNManifestGenerator } from './components/CDNManifestGenerator';

// In your route config or nav:
<Route path="/cdn-manifest" element={<CDNManifestGenerator />} />
```

### 4. Add nav link

```tsx
{/* In your main navigation */}
<a href="/cdn-manifest" className="nav-link">
  CDN Manifest
</a>
```

## Usage

1. Navigate to `/cdn-manifest`
2. Enter repo ID (e.g., `axentx/surrogate-datasets`) and date folder (e.g., `2026-04-29`)
3. Click "Generate Manifest" — single API call to HF tree endpoint
4. Download JSON manifest and embed in Lightning training script for CDN-only fetches

## Training Script Integration (for reference)

```python
# In Lightning training script
import json
with open('manifest.json') as f:
    manifest = json.load(f)

