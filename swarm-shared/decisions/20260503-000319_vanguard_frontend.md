# vanguard / frontend

## Final Synthesized Implementation

### Diagnosis (merged)
- No persisted HF file manifest per `(repo, dateFolder)` in the frontend; every training launch re-enumerates via authenticated API → quota burn and 429 risk.
- Training UI cannot pre-flight or cache available files; users launch blind and risk mid-run failures.
- Missing CDN-only strategy: training script still relies on `load_dataset(streaming=True)` which triggers authenticated API calls and fails on heterogeneous schemas.
- No stable file-list artifact shared between frontend (launch) and training.
- No guard against invoking training when the manifest is empty/stale → immediate runtime failure.

### Scope
`/opt/axentx/vanguard/src/frontend/` — add `FileManifestManager.ts`, update training launch view and API adapter.

---

### File: `src/frontend/lib/FileManifestManager.ts`
```ts
// Lightweight manifest manager for HF repos + date folders (CDN-only strategy)
// Uses localStorage for immediate frontend wins; backend can later persist to repo.

export interface HfFileEntry {
  path: string;        // relative path in repo
  cdnUrl: string;      // https://huggingface.co/datasets/{repo}/resolve/main/{path}
  size?: number;       // optional size from tree
  type?: 'file' | 'dir';
}

export interface RepoDateManifest {
  repo: string;        // e.g. "datasets/my-corpus"
  dateFolder: string;  // e.g. "2026-04-29"
  createdAt: string;   // ISO when manifest saved
  files: HfFileEntry[];
  fileCount: number;
}

const STORAGE_PREFIX = 'vanguard:hf-manifest:';

export class FileManifestManager {
  static key(repo: string, dateFolder: string): string {
    return `${STORAGE_PREFIX}${repo}:${dateFolder}`;
  }

  static save(manifest: RepoDateManifest): void {
    try {
      localStorage.setItem(this.key(manifest.repo, manifest.dateFolder), JSON.stringify(manifest));
    } catch (e) {
      console.warn('[FileManifestManager] localStorage save failed', e);
    }
  }

  static load(repo: string, dateFolder: string): RepoDateManifest | null {
    try {
      const raw = localStorage.getItem(this.key(repo, dateFolder));
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      console.warn('[FileManifestManager] localStorage load failed', e);
      return null;
    }
  }

  static listAvailable(repo: string): RepoDateManifest[] {
    try {
      const result: RepoDateManifest[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith(`${STORAGE_PREFIX}${repo}:`)) {
          const raw = localStorage.getItem(k);
          if (raw) {
            try { result.push(JSON.parse(raw)); } catch { /* skip corrupt */ }
          }
        }
      }
      // newest first
      return result.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    } catch {
      return [];
    }
  }

  static remove(repo: string, dateFolder: string): void {
    localStorage.removeItem(this.key(repo, dateFolder));
  }

  // Build CDN-only URLs for a known folder (no auth, bypasses /api/ rate limits)
  static buildCdnUrls(repo: string, dateFolder: string, filePaths: string[]): HfFileEntry[] {
    const base = `https://huggingface.co/datasets/${repo}/resolve/main`;
    return filePaths.map((path) => ({
      path,
      cdnUrl: `${base}/${encodeURIComponent(path)}`,
      type: 'file',
    }));
  }

  // Create and persist manifest from repo + dateFolder + enumerated file paths
  static createFromPaths(repo: string, dateFolder: string, filePaths: string[]): RepoDateManifest {
    const files = this.buildCdnUrls(repo, dateFolder, filePaths);
    const manifest: RepoDateManifest = {
      repo,
      dateFolder,
      createdAt: new Date().toISOString(),
      files,
      fileCount: files.length,
    };
    this.save(manifest);
    return manifest;
  }
}
```

---

### File: `src/frontend/components/TrainingLaunch.tsx`
```tsx
import React, { useState, useEffect } from 'react';
import { FileManifestManager, type RepoDateManifest } from '../lib/FileManifestManager';

export function TrainingLaunch() {
  const [repo, setRepo] = useState('datasets/my-corpus');
  const [dateFolder, setDateFolder] = useState('');
  const [manifests, setManifests] = useState<RepoDateManifest[]>([]);
  const [selectedManifest, setSelectedManifest] = useState<RepoDateManifest | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setManifests(FileManifestManager.listAvailable(repo));
  }, [repo]);

  const onLoadManifest = () => {
    const m = FileManifestManager.load(repo, dateFolder);
    if (m) {
      setSelectedManifest(m);
    } else {
      alert('No saved manifest for this folder. Use "Refresh manifest" (single API call) first.');
    }
  };

  const onRefreshManifest = async () => {
    if (!dateFolder.trim()) {
      alert('Enter a date folder (e.g. 2026-04-29)');
      return;
    }
    setLoading(true);
    try {
      // Lightweight: ask backend to list one dateFolder (single API call) and return paths.
      // Backend should call list_repo_tree(path=dateFolder, recursive=false) and return paths.
      const res = await fetch(`/api/hf/list?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(dateFolder)}`);
      if (!res.ok) throw new Error('Failed to list folder');
      const { paths }: { paths: string[] } = await res.json();
      if (!Array.isArray(paths) || paths.length === 0) {
        alert('No files found in folder');
        return;
      }
      const manifest = FileManifestManager.createFromPaths(repo, dateFolder, paths);
      setSelectedManifest(manifest);
      setManifests(FileManifestManager.listAvailable(repo));
    } catch (err) {
      console.error(err);
      alert('Failed to refresh manifest');
    } finally {
      setLoading(false);
    }
  };

  const onLaunchTraining = async () => {
    if (!selectedManifest || selectedManifest.files.length === 0) {
      alert('No files selected. Refresh manifest and pick a date folder.');
      return;
    }
    // Embed CDN-only file list into training payload so Lightning training does zero HF API calls.
    const payload = {
      repo: selectedManifest.repo,
      dateFolder: selectedManifest.dateFolder,
      filePaths: selectedManifest.files.map((f) => f.path),
      cdnBase: `https://huggingface.co/datasets/${selectedManifest.repo}/resolve/main`,
      strategy: 'cdn-only' as const,
    };

    try {
      const res = await fetch('/api/training/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('Launch failed');
      alert('Training launched (CDN-only)');
    } catch (err) {
      console.error(err);
      alert('Launch failed');
    }
  };

  return (
    <div>
      <h3>Launch Surrogate-1 Training (CDN-only)</h3>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <label>
          Repo:{' '}
          <input value={repo} onChange={(e) => setRepo(e.target.value)} />
        </label>
        <label>
          Date folder:{' '}
          <input value={dateFolder} onChange={(e) => setDateFolder(e.target.value)} />
        </label>
        <button onClick
