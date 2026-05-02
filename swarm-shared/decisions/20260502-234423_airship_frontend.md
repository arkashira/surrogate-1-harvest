# airship / frontend

# Airship Frontend — Highest-Value Incremental (<2h)

## Chosen improvement
Add a lightweight frontend page (`/discover`) that shows the latest CDN-only manifest produced by `airship discover` and exposes a “Copy CDN train script” button.  
This unblocks immediate surrogate-1 training iterations by giving users a deterministic, zero-API-during-training file list and a ready-to-run Lightning script — no backend changes required.

## Why this is highest value
- Directly applies the **HF CDN bypass** and **pre-list file paths once** patterns.
- Enables users to start surrogate-1 training immediately without waiting on backend work.
- Small, self-contained frontend change (<2h) with clear user-facing outcome.

---

## Implementation plan

1. Add route `/discover` in frontend router.
2. Create `DiscoverPage` component:
   - Input: `repo_id` (default from context or placeholder) and `date_folder` (YYYY-MM-DD).
   - On mount (or on form submit) fetch `/api/discover?repo_id=...&date_folder=...` (existing or new lightweight endpoint — if missing, mock with static sample for now).
   - Display manifest as a table (path, size, ext).
   - Show “Copy CDN train script” button that copies a generated `train.py` using CDN-only URLs.
3. Add utility to generate CDN URLs:
   - `https://huggingface.co/datasets/{repo_id}/resolve/main/{date_folder}/{path}`
4. Add utility to generate Lightning train script:
   - Uses `Teamspace.studios` reuse pattern and `Machine.L40S`.
   - Reads file list from embedded JSON (simulated) and streams via CDN.
5. Polish: loading states, error states, copy-toast.

---

## Code snippets

### Frontend route (React + TanStack Router example)

```tsx
// src/routes/discover.route.tsx
import { createFileRoute } from '@tanstack/react-router';
import { DiscoverPage } from '@/pages/DiscoverPage';

export const Route = createFileRoute('/discover')({
  component: DiscoverPage,
});
```

### DiscoverPage component

```tsx
// src/pages/DiscoverPage.tsx
import { useState, useEffect } from 'react';
import { useSearchParams } from '@tanstack/react-router';
import { generateCdnUrl, generateLightningTrainScript } from '@/lib/cdn-utils';

type ManifestItem = {
  path: string;
  size: number;
  ext: string;
};

export function DiscoverPage() {
  const [searchParams, setSearchParams] = useSearchParams({
    repo_id: 'myorg/surrogate-data',
    date_folder: new Date().toISOString().slice(0, 10),
  });

  const [manifest, setManifest] = useState<ManifestItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function fetchManifest(repoId: string, dateFolder: string) {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/discover?repo_id=${encodeURIComponent(repoId)}&date_folder=${encodeURIComponent(dateFolder)}`
      );
      if (!res.ok) throw new Error('Failed to fetch manifest');
      const json = await res.json();
      setManifest(json.files || []);
    } catch (err: any) {
      setError(err.message);
      setManifest(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchManifest(searchParams.repo_id, searchParams.date_folder);
  }, [searchParams.repo_id, searchParams.date_folder]);

  function handleCopyScript() {
    if (!manifest) return;
    const script = generateLightningTrainScript({
      repoId: searchParams.repo_id,
      dateFolder: searchParams.date_folder,
      files: manifest,
    });
    navigator.clipboard.writeText(script).then(() => {
      alert('Train script copied to clipboard');
    });
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-4">CDN Manifest Discover</h1>

      <div className="flex gap-2 mb-4">
        <input
          className="border rounded px-2 py-1"
          placeholder="repo_id (e.g., org/repo)"
          value={searchParams.repo_id}
          onChange={(e) =>
            setSearchParams({ repo_id: e.target.value, date_folder: searchParams.date_folder })
          }
        />
        <input
          className="border rounded px-2 py-1"
          placeholder="date_folder (YYYY-MM-DD)"
          value={searchParams.date_folder}
          onChange={(e) =>
            setSearchParams({ repo_id: searchParams.repo_id, date_folder: e.target.value })
          }
        />
        <button
          className="bg-blue-600 text-white rounded px-4 py-1"
          onClick={() => fetchManifest(searchParams.repo_id, searchParams.date_folder)}
          disabled={loading}
        >
          {loading ? 'Loading...' : 'Discover'}
        </button>
      </div>

      {error && <div className="text-red-600 mb-4">{error}</div>}

      {manifest && manifest.length > 0 && (
        <>
          <div className="flex justify-between items-center mb-2">
            <span className="text-sm text-gray-600">{manifest.length} files</span>
            <button
              className="bg-green-700 text-white rounded px-4 py-1 text-sm"
              onClick={handleCopyScript}
            >
              Copy CDN train script
            </button>
          </div>

          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b">
                <th className="text-left p-2">Path</th>
                <th className="text-right p-2">Size</th>
                <th className="text-right p-2">Ext</th>
                <th className="text-left p-2">CDN URL</th>
              </tr>
            </thead>
            <tbody>
              {manifest.map((item, idx) => (
                <tr key={idx} className="border-b">
                  <td className="p-2 font-mono text-xs">{item.path}</td>
                  <td className="p-2 text-right">{(item.size / 1024).toFixed(1)} KB</td>
                  <td className="p-2 text-right">{item.ext}</td>
                  <td className="p-2">
                    <a
                      href={generateCdnUrl(searchParams.repo_id, searchParams.date_folder, item.path)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-600 underline text-xs"
                    >
                      open
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {manifest && manifest.length === 0 && !loading && (
        <div className="text-gray-500">No files found for this folder.</div>
      )}
    </div>
  );
}
```

### CDN utilities

```ts
// src/lib/cdn-utils.ts

export function generateCdnUrl(repoId: string, dateFolder: string, filePath: string): string {
  const cleanRepo = repoId.replace(/^\/?datasets\//, '');
  return `https://huggingface.co/datasets/${cleanRepo}/resolve/main/${dateFolder}/${filePath}`;
}

export function generateLightningTrainScript(opts: {
  repoId: string;
  dateFolder: string;
  files: Array<{ path: string }>;
}): string {
  const { repoId, dateFolder, files } = opts;
  const fileListJson = JSON.stringify(files.map((f) => f.path), null, 2);
  const repoDisplay = repoId.replace(/\//g, '_');

  return `import lightning as L
import torch
from torch.utils.data import IterableDataset, DataLoader
import json
import requests
from pathlib import Path

# CDN-only file list (no HF API calls during training)
FILE_LIST
