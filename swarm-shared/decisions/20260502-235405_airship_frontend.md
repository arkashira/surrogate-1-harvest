# airship / frontend

## Implementation Plan — Airship Frontend Discover Page

**Scope**: ≤2h  
**Goal**: Add a React Discover page in Arkship (port 3000) that calls the existing `/api/discover` backend, shows deterministic HF CDN file manifests, and supports copy-to-clipboard + download as JSON.

### Why this is highest-value
- Unblocks surrogate/kaggle training pipelines immediately (uses the CDN bypass pattern).
- Reuses existing backend (`/api/discover`) — no backend changes required.
- Small, self-contained UI (one new page + route + hook) — ships in <2h.

---

### Implementation Steps

1. Add route `/discover` in Arkship frontend router.
2. Create `DiscoverPage` component:
   - Form: `repoId`, `dateFolder` (optional), `recursive` checkbox.
   - Submit → GET `/api/discover?repoId=...&dateFolder=...&recursive=...`
   - Show loading, error, and results table (path, size, type, cdnUrl).
   - Actions: Copy manifest JSON, Download `.json` file.
3. Add simple hook `useDiscover` to encapsulate API call.
4. Wire into main navigation (optional: add nav item or expose via sidebar).

---

### Code Snippets

#### 1. Route addition (likely in `arkship/src/App.tsx` or router file)
```tsx
// arkship/src/App.tsx  (or equivalent router)
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import DiscoverPage from './pages/DiscoverPage/DiscoverPage';

function App() {
  return (
    <Router>
      <Routes>
        {/* existing routes */}
        <Route path="/discover" element={<DiscoverPage />} />
      </Routes>
    </Router>
  );
}
```

#### 2. Hook: `useDiscover`
```ts
// arkship/src/hooks/useDiscover.ts
import { useState, useCallback } from 'react';

export interface FileEntry {
  path: string;
  size: number;
  type: 'file' | 'directory';
  cdnUrl?: string;
}

export interface DiscoverResult {
  repoId: string;
  dateFolder?: string;
  recursive: boolean;
  files: FileEntry[];
  generatedAt: string;
}

export function useDiscover() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DiscoverResult | null>(null);

  const discover = useCallback(async (repoId: string, dateFolder?: string, recursive = false) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ repoId, recursive: String(recursive) });
      if (dateFolder) params.set('dateFolder', dateFolder);

      const res = await fetch(`/api/discover?${params.toString()}`, {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
      });

      if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
      const data = (await res.json()) as DiscoverResult;
      setResult(data);
    } catch (err: any) {
      setError(err.message || 'Failed to fetch manifest');
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, []);

  return { loading, error, result, discover };
}
```

#### 3. Component: `DiscoverPage`
```tsx
// arkship/src/pages/DiscoverPage/DiscoverPage.tsx
import React, { useState } from 'react';
import { useDiscover, DiscoverResult } from '../../hooks/useDiscover';

function downloadJson(obj: any, filename: string) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function DiscoverPage() {
  const [repoId, setRepoId] = useState('');
  const [dateFolder, setDateFolder] = useState('');
  const [recursive, setRecursive] = useState(false);
  const { loading, error, result, discover } = useDiscover();

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!repoId.trim()) return;
    discover(repoId.trim(), dateFolder.trim() || undefined, recursive);
  };

  const handleCopy = () => {
    if (!result) return;
    navigator.clipboard.writeText(JSON.stringify(result, null, 2)).catch(() => {
      alert('Failed to copy');
    });
  };

  const handleDownload = () => {
    if (!result) return;
    const safeRepo = repoId.replace(/[\s/]/g, '_');
    const folderPart = dateFolder ? `_${dateFolder}` : '';
    downloadJson(result, `manifest_${safeRepo}${folderPart}.json`);
  };

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: 24 }}>
      <h1>Discover — HF CDN File Manifest</h1>
      <p>
        Generate a deterministic CDN-only file manifest for a Hugging Face dataset repo.
        Uses CDN bypass (no HF API auth) for training pipelines.
      </p>

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <input
          placeholder="Repo ID (e.g., 'datasets/my-repo')"
          value={repoId}
          onChange={(e) => setRepoId(e.target.value)}
          style={{ flex: 1, minWidth: 200, padding: 8 }}
          required
        />
        <input
          placeholder="Date folder (optional, e.g., '2026-04-29')"
          value={dateFolder}
          onChange={(e) => setDateFolder(e.target.value)}
          style={{ flex: 1, minWidth: 160, padding: 8 }}
        />
        <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} />
          Recursive
        </label>
        <button type="submit" disabled={loading || !repoId.trim()} style={{ padding: '8 16px' }}>
          {loading ? 'Discovering...' : 'Discover'}
        </button>
      </form>

      {error && <div style={{ color: 'red', marginBottom: 12 }}>{error}</div>}

      {!loading && result && (
        <div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
            <button onClick={handleCopy}>Copy JSON</button>
            <button onClick={handleDownload}>Download JSON</button>
          </div>

          <h3>Manifest: {result.repoId}{result.dateFolder ? ` / ${result.dateFolder}` : ''}</h3>
          <p style={{ color: '#666', fontSize: 13 }}>
            Generated at {result.generatedAt} — recursive: {String(result.recursive)} — files: {result.files.length}
          </p>

          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #ddd' }}>
                <th style={{ textAlign: 'left', padding: 8 }}>Path</th>
                <th style={{ textAlign: 'right', padding: 8 }}>Size</th>
                <th style={{ textAlign: 'center', padding: 8 }}>Type</th>
                <th style={{ textAlign: 'left', padding: 8 }}>CDN URL</th>
              </tr>
            </thead>
            <tbody>
              {result.files.map((f, i) => (
               
