# airship / frontend

## Implementation Plan — `airship discover` (frontend)

**Scope**: ≤2h  
**Goal**: Add frontend UI for `airship discover` so users can request a CDN-only file manifest for a HuggingFace dataset (`repo_id` + `date_folder`) and view/download the JSON manifest. Integrates with the backend endpoint planned in prior decisions.

### What I’m shipping (highest-value incremental)
- A single-page UI at `/discover` (or modal in Arkship UI) that:
  - Accepts `repo_id` and `date_folder`
  - Calls `GET /api/discover?repo_id=...&date_folder=...`
  - Shows progress, success/error, and the resulting manifest (JSON)
  - Offers download of the manifest file
- Minimal, accessible React component + route + service layer (no heavy state libs)
- Uses CDN bypass pattern: backend will do one `list_repo_tree` then return CDN URLs; frontend only renders and fetches result.

### Implementation steps (≤2h)
1. Add frontend route `/discover` (React Router)
2. Create `DiscoverPage` component with form + results view
3. Add `discoverService` to call `/api/discover`
4. Wire into Arkship nav (optional) or expose as standalone page
5. Basic error handling and loading states
6. Manifest download button (JSON)

### Code snippets

#### 1) Frontend route (React Router)
```tsx
// arkship/src/routes.tsx
import { createBrowserRouter } from 'react-router-dom';
import DiscoverPage from './pages/DiscoverPage';
import ArkshipLayout from './layouts/ArkshipLayout';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <ArkshipLayout />,
    children: [
      // ...other routes
      { path: 'discover', element: <DiscoverPage /> }
    ]
  }
]);
```

#### 2) Discover service (lightweight)
```ts
// arkship/src/services/discoverService.ts
export interface DiscoverParams {
  repo_id: string;
  date_folder: string;
}

export interface DiscoverResult {
  repo_id: string;
  date_folder: string;
  files: Array<{
    path: string;
    cdn_url: string;
    size?: number;
    sha256?: string;
  }>;
  generated_at: string;
}

export async function discoverManifest(
  params: DiscoverParams,
  signal?: AbortSignal
): Promise<DiscoverResult> {
  const qs = new URLSearchParams({
    repo_id: params.repo_id,
    date_folder: params.date_folder
  });

  const res = await fetch(`/api/discover?${qs}`, { signal });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.message || `Failed to discover manifest (${res.status})`);
  }
  return res.json();
}
```

#### 3) DiscoverPage component
```tsx
// arkship/src/pages/DiscoverPage.tsx
import { useState } from 'react';
import { discoverManifest, type DiscoverResult } from '../services/discoverService';

export default function DiscoverPage() {
  const [repoId, setRepoId] = useState('');
  const [dateFolder, setDateFolder] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<DiscoverResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const data = await discoverManifest({ repo_id: repoId, date_folder: dateFolder });
      setResult(data);
    } catch (err: any) {
      setError(err.message || 'Unknown error');
    } finally {
      setLoading(false);
    }
  }

  function downloadManifest() {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${result.repo_id.replace(/\//g, '_')}_${result.date_folder}_manifest.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="max-w-3xl mx-auto p-6">
      <h1 className="text-2xl font-semibold mb-4">Discover CDN Manifest</h1>
      <p className="text-sm text-gray-600 mb-6">
        Request a CDN-only file manifest for a HuggingFace dataset date folder. Backend performs a single list_repo_tree
        and returns CDN URLs (bypasses HF API rate limits during training).
      </p>

      <form onSubmit={handleSubmit} className="space-y-4 mb-6">
        <div>
          <label className="block text-sm font-medium mb-1">Repo ID</label>
          <input
            className="w-full px-3 py-2 border rounded"
            placeholder="datasets/my-org/my-repo"
            value={repoId}
            onChange={(e) => setRepoId(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Date folder</label>
          <input
            className="w-full px-3 py-2 border rounded"
            placeholder="2026-04-29"
            value={dateFolder}
            onChange={(e) => setDateFolder(e.target.value)}
            required
          />
        </div>
        <button
          type="submit"
          disabled={loading || !repoId || !dateFolder}
          className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {loading ? 'Discovering...' : 'Discover'}
        </button>
      </form>

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 text-red-800 rounded">
          {error}
        </div>
      )}

      {result && (
        <div className="border rounded p-4 bg-gray-50">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-medium">Manifest: {result.repo_id} / {result.date_folder}</h2>
            <button
              onClick={downloadManifest}
              className="px-3 py-1 text-sm bg-green-600 text-white rounded"
            >
              Download JSON
            </button>
          </div>
          <p className="text-xs text-gray-500 mb-3">Generated: {result.generated_at}</p>
          <div className="text-sm">
            <p className="font-medium mb-1">Files ({result.files.length})</p>
            <ul className="max-h-60 overflow-auto border rounded p-2 bg-white">
              {result.files.map((f, i) => (
                <li key={i} className="flex justify-between gap-4 py-1 border-b last:border-b-0">
                  <span className="truncate">{f.path}</span>
                  <span className="text-xs text-gray-500 shrink-0">{f.size ? `${(f.size / 1024).toFixed(1)} KB` : ''}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
```

#### 4) Add nav link (optional)
```tsx
// arkship/src/components/Nav.tsx
<li>
  <a href="/discover" className="hover:underline">Discover</a>
</li>
```

### Notes & Best Practices
- Uses CDN bypass pattern: backend does the single `list_repo_tree` and returns CDN URLs; frontend never calls HF API.
- AbortController support included in service for cancellation.
- Minimal dependencies — only React + fetch.
- If Arkship backend isn’t ready, mock the endpoint temporarily with a fixture to unblock frontend testing.

