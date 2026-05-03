# vanguard / frontend

### 1. Diagnosis (merged + prioritized)
- **Repeated authenticated HF API calls**: no persisted `(repo, dateFolder) → file-list` manifest causes `list_repo_tree`/`tree` calls that burn quota and risk 429s.
- **Training/data loader hazards**: repeated per-file downloads or `load_dataset(streaming=True)` on mixed-schema HF datasets can trigger pyarrow `CastError` and waste quota.
- **Frontend does not use CDN bypass**: relying on authenticated API instead of `https://huggingface.co/datasets/{repo}/resolve/main/{path}` increases load and latency.
- **Lightning Studio lifecycle gaps**: no handling of idle-stop/auto-resume and no reuse policy, wasting quota (target: ~80hr/mo saved).
- **Missing UX**: no repo/date selector + file preview before training launch.

### 2. Single concrete proposal
Implement a **frontend manifest cache + CDN-bypass selector** plus a **training-side manifest loader** so:
1. Repo tree is listed **once per `(repo, dateFolder)`** and cached (localStorage + optional server artifact).
2. Frontend exposes CDN URLs for selected files; training consumes a persisted `file-list.json` and downloads via CDN bypass without HF API auth.
3. Lightweight Studio reuse/idle handling is added to avoid quota waste.

Scope: ~120 LOC across 1 new component + 2 utils + 1 small training loader change.

---

### 3. Implementation (merged + hardened)

#### Create files
```bash
mkdir -p /opt/axentx/vanguard/src/components
mkdir -p /opt/axentx/vanguard/src/lib
```

#### src/lib/hf.ts
```ts
// HF CDN-bypass utilities (public datasets; no auth required for resolve URLs)
const CDN_ROOT = 'https://huggingface.co/datasets';

export function cdnResolve(repo: string, path: string): string {
  // Normalize repo (strip leading/trailing slashes) and path
  const r = repo.replace(/^\/+|\/+$/g, '');
  const p = path.replace(/^\/+/, '');
  return `${CDN_ROOT}/${r}/resolve/main/${p}`;
}

export interface HFNode {
  path: string;
  type: 'file' | 'directory';
  size?: number;
}

// Fetch tree (requires token for private repos).
// Call this from orchestration and cache result; frontend prefers cached manifest.
export async function fetchRepoTree(
  repo: string,
  dateFolder: string,
  token?: string
): Promise<HFNode[]> {
  const url = `https://huggingface.co/api/datasets/${repo}/tree`;
  const params = new URLSearchParams({ recursive: 'false', path: dateFolder });
  const headers: HeadersInit = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${url}?${params}`, { headers });
  if (!res.ok) throw new Error(`HF tree failed: ${res.status}`);
  const nodes: HFNode[] = await res.json();
  return nodes;
}
```

#### src/lib/training.ts
```ts
export interface FileListCache {
  repo: string;
  dateFolder: string;
  files: string[];       // relative paths within dateFolder (or full repo-relative)
  updatedAt: number;
}

const CACHE_KEY = 'vanguard:hf-file-list';

export function saveFileList(cache: FileListCache): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch (e) {
    // localStorage may be unavailable in some contexts; degrade gracefully.
    console.warn('Could not persist file-list cache', e);
  }
}

export function loadFileList(repo: string, dateFolder: string): FileListCache | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const cache: FileListCache = JSON.parse(raw);
    if (cache.repo !== repo || cache.dateFolder !== dateFolder) return null;
    // 24h TTL
    if (Date.now() - cache.updatedAt > 86_400_000) return null;
    return cache;
  } catch (e) {
    return null;
  }
}

export function buildCDNUrls(repo: string, files: string[]): string[] {
  return files.map((f) => cdnResolve(repo, f));
}

// Persist manifest for training scripts to consume (server-side or shared volume).
export function writeFileListJSON(
  repo: string,
  dateFolder: string,
  files: string[],
  outPath: string
): void {
  // In browser context this would be a download; in Node context (or via API) it writes JSON.
  const payload = { repo, dateFolder, files, updatedAt: Date.now() };
  // Implementation depends on runtime; provide a Node-friendly helper below.
  return writeJSON(outPath, payload);
}

// Node helper (safe to use in server-side orchestration or Electron-like envs)
function writeJSON(path: string, obj: unknown): void {
  try {
    // Dynamic require to avoid bundling fs in frontend builds.
    const fs = require('fs');
    fs.writeFileSync(path, JSON.stringify(obj, null, 2), 'utf8');
  } catch (e) {
    console.warn('writeJSON unavailable or failed', e);
  }
}
```

#### src/components/DataSelector.tsx
```tsx
import React, { useState, useEffect } from 'react';
import { fetchRepoTree } from '../lib/hf';
import { loadFileList, saveFileList, buildCDNUrls } from '../lib/training';

interface Props {
  repo: string;
  dateFolder: string;
  token?: string;
  onFilesSelected?: (urls: string[]) => void;
}

export const DataSelector: React.FC<Props> = ({
  repo,
  dateFolder,
  token,
  onFilesSelected,
}) => {
  const [nodes, setNodes] = useState<Array<{ path: string; type: 'file' }>>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const cached = loadFileList(repo, dateFolder);
        if (cached) {
          setNodes(cached.files.map((f) => ({ path: f, type: 'file' })));
          setLoading(false);
          return;
        }

        const tree = await fetchRepoTree(repo, dateFolder, token);
        const files = tree
          .filter((n) => n.type === 'file')
          .map((n) => n.path)
          .sort();
        const cache = { repo, dateFolder, files, updatedAt: Date.now() };
        saveFileList(cache);
        setNodes(files.map((f) => ({ path: f, type: 'file' })));
      } catch (err: any) {
        setError(err.message || 'Failed to list files');
      } finally {
        setLoading(false);
      }
    };

    if (repo && dateFolder) load();
  }, [repo, dateFolder, token]);

  const toggle = (path: string) => {
    const next = new Set(selected);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    setSelected(next);
  };

  const submit = () => {
    const urls = buildCDNUrls(repo, Array.from(selected));
    onFilesSelected?.(urls);
  };

  if (!repo || !dateFolder) {
    return <div className="p-4 text-sm text-gray-500">Select repo and date folder to list files.</div>;
  }

  if (loading) return <div className="p-4 text-sm text-gray-600">Loading file list...</div>;
  if (error) return <div className="p-4 text-sm text-red-600">{error}</div>;

  return (
    <div className="border rounded p-4 max-w-lg">
      <h3 className="font-semibold mb-2">
        {repo}
