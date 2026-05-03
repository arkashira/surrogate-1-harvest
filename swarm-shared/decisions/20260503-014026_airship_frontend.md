# airship / frontend

**SINGLE SYNTHESIZED PLAN (Best Parts + Resolved Contradictions)**

## 1. Highest-Value Increment (< 2 h)
**Frontend: Dataset Manifest Generator + CDN-Bypass Training Trigger**  
- **Why**: Directly eliminates HF 429s for Surrogate training (shared #1 pain point).  
- **Scope**: One new React component + two backend proxy endpoints.  
- **Correctness fix**: Use **single `list_repo_tree` call** (not per-file) to generate a manifest once, then reuse it for Lightning training with CDN-only URLs. No per-file API hits → no 429s.

## 2. Concrete Implementation (Actionable)

### 2.1 Frontend Component (45–60 min)
File: `arkship/frontend/src/components/DatasetManifestGenerator.tsx`

```tsx
import React, { useState, useEffect } from 'react';
import { Play, RefreshCw, HardDrive, AlertCircle } from 'lucide-react';

interface RepoTree {
  repo: string;
  path: string;
  files: { path: string; size: number }[];
}

interface TrainingJob {
  id: string;
  repo: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  progress: number;
}

export const DatasetManifestGenerator: React.FC = () => {
  const [repos, setRepos] = useState<RepoTree[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string>('');
  const [jobs, setJobs] = useState<TrainingJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchRepos = async () => {
    try {
      const res = await fetch('http://localhost:8001/api/hf/repos');
      if (!res.ok) throw new Error('Failed to fetch repos');
      setRepos(await res.json());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const generateAndStart = async () => {
    if (!selectedRepo) return;
    setLoading(true);
    setError(null);

    try {
      // 1) Single list_repo_tree call (backend proxy)
      const treeRes = await fetch('http://localhost:8001/api/hf/tree', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo: selectedRepo, recursive: false }),
      });
      if (!treeRes.ok) throw new Error('Tree fetch failed');
      const tree = await treeRes.json();

      // 2) Persist manifest (backend proxy)
      const manifestRes = await fetch('http://localhost:8001/api/training/manifest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo: selectedRepo, tree }),
      });
      if (!manifestRes.ok) throw new Error('Manifest save failed');
      const manifest = await manifestRes.json();

      // 3) Start Lightning training with CDN-only URLs
      const trainRes = await fetch('http://localhost:8001/api/training/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          manifestId: manifest.id,
          reuseRunningStudio: true,
          machine: 'L40S',
          cloud: 'lightning-public-prod',
          cdnOnly: true,
        }),
      });
      if (!trainRes.ok) throw new Error('Training start failed');
      const job = await trainRes.json();

      setJobs((prev) => [...prev, job]);
      fetchRepos();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchRepos();
  }, []);

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <HardDrive className="w-5 h-5 text-blue-600" />
          <h2 className="font-semibold">Dataset Manifest Generator</h2>
        </div>
        <button
          onClick={fetchRepos}
          className="p-1.5 hover:bg-gray-100 rounded"
          title="Refresh"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {error && (
        <div className="mb-3 flex items-center gap-2 text-red-600 text-sm">
          <AlertCircle className="w-4 h-4" />
          {error}
        </div>
      )}

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Select repo to train</label>
        <div className="flex gap-2">
          <select
            value={selectedRepo}
            onChange={(e) => setSelectedRepo(e.target.value)}
            className="flex-1 border rounded px-3 py-2 text-sm"
          >
            <option value="">— choose repo —</option>
            {repos.map((r) => (
              <option key={`${r.repo}-${r.path}`} value={r.repo}>
                {r.repo} ({r.files.length} files)
              </option>
            ))}
          </select>
          <button
            onClick={generateAndStart}
            disabled={!selectedRepo || loading}
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-400 flex items-center gap-2"
          >
            <Play className="w-4 h-4" />
            {loading ? 'Starting…' : 'Start Training'}
          </button>
        </div>
      </div>

      {jobs.length > 0 && (
        <div className="border-t pt-3">
          <h3 className="text-sm font-medium mb-2">Recent Jobs</h3>
          <ul className="space-y-1 text-sm">
            {jobs.map((j) => (
              <li key={j.id} className="flex items-center justify-between">
                <span>{j.repo}</span>
                <span
                  className={
                    j.status === 'running'
                      ? 'text-green-600'
                      : j.status === 'completed'
                      ? 'text-blue-600'
                      : j.status === 'failed'
                      ? 'text-red-600'
                      : 'text-yellow-600'
                  }
                >
                  {j.status}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};
```

### 2.2 Backend Proxy Endpoints (60–75 min)

File: `arkship/backend/src/routes/hf.ts`

```ts
import { Router } from 'express';
import axios from 'axios';

const router = Router();

// Fetch repo list (lightweight)
router.get('/repos', async (req, res) => {
  try {
    // Replace with actual source (DB or config). Example static list:
    res.json([
      { repo: 'myorg/train-ds-1', path: 'data/', files: [] },
      { repo: 'myorg/train-ds-2', path: 'parquet/', files: [] },
    ]);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Single list_repo_tree call (CDN-bypass enabler)
router.post('/tree', async (req, res) => {
  try {
    const { repo, recursive = false } = req.body;
    if (!repo) return res.status(400).json({ error: 'repo required' });

    // Call Hugging Face Hub API (server-side to avoid client 429s)
    const treeRes = await axios.post(
      `https://hugging
