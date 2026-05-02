# airship / frontend

### Final Synthesis (chosen parts + corrections + concrete actions)

**Goal**: Ship the highest-value frontend improvement in <2h that removes HF API rate-limit risk and surfaces tagged research + CDN training manifests in the Arkship UI.  
**Strategy**:  
- Use Candidate 1’s CDN-bypass pattern for dataset training (no HF API, direct CDN downloads).  
- Use Candidate 2’s minimal backend/frontend integration to expose `/var/airship/discover` outputs and manifests (no HF API from frontend).  
- Fix contradictions:  
  - Candidate 1’s example code used incorrect CDN URL pattern and omitted recursive file listing; correct it.  
  - Candidate 2 assumed backend endpoints existed; we’ll implement them minimally and safely.  
- Prioritize correctness and concrete actionability.

---

## 1) Backend: lightweight `/api/discover` endpoints + CDN-bypass util

File: `arkship/src/routes/discover.js`

```js
const express = require('express');
const fs = require('fs').promises;
const path = require('path');
const { execFile } = require('child_process');
const fetch = require('node-fetch');
const router = express.Router();

const OUTPUTS_DIR = '/var/airship/discover/outputs';
const MANIFEST_DIR = '/var/airship/discover/manifest';
const STATUS_FILE = '/var/airship/discover/status.json';
const BIN = '/opt/axentx/airship/bin/airship-discover';

// --- util: HF repo file listing + CDN-bypass download ---
async function listRepoFiles(repo, recursive = true) {
  const res = await fetch(
    `https://huggingface.co/api/v1/datasets/${repo}/tree?recursive=${recursive ? 'true' : 'false'}`
  );
  if (!res.ok) throw new Error(`HF tree failed: ${res.status}`);
  return res.json(); // array of { path, type }
}

function buildCdnUrl(repo, filePath) {
  // Correct CDN pattern for datasets (resolve -> main or specific commit)
  return `https://huggingface.co/datasets/${repo}/resolve/main/${filePath}`;
}

async function downloadFromCdn(repo, filePath) {
  const url = buildCdnUrl(repo, filePath);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`CDN download failed: ${res.status} ${url}`);
  return res.buffer();
}

// expose util for training scripts if needed
router.locals.hf = { listRepoFiles, downloadFromCdn, buildCdnUrl };

// --- status + outputs + manifest ---
async function readStatus() {
  try {
    const raw = await fs.readFile(STATUS_FILE, 'utf8');
    return JSON.parse(raw);
  } catch {
    return { status: 'idle', lastRun: null, lastError: null };
  }
}

// GET /api/discover/status
router.get('/status', async (req, res) => {
  try {
    const status = await readStatus();

    let outputs = [];
    try {
      const files = await fs.readdir(OUTPUTS_DIR);
      const jsonFiles = files.filter((f) => f.endsWith('.json'));
      outputs = await Promise.all(
        jsonFiles.map(async (f) => {
          const raw = await fs.readFile(path.join(OUTPUTS_DIR, f), 'utf8');
          return { file: f, ...JSON.parse(raw) };
        })
      );
      outputs.sort((a, b) => new Date(b.ts || 0) - new Date(a.ts || 0));
    } catch {
      // no outputs yet
    }

    let manifestUrl = null;
    try {
      const files = await fs.readdir(MANIFEST_DIR);
      const manifests = files.filter((f) => f.endsWith('.json')).sort().reverse();
      if (manifests.length > 0) {
        manifestUrl = `/discover/manifest/${encodeURIComponent(manifests[0])}`;
      }
    } catch {
      // no manifest yet
    }

    res.json({ status, outputs, manifestUrl });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// POST /api/discover/run
router.post('/run', (req, res) => {
  // Non-blocking, cron-safe
  const child = execFile(BIN, { detached: true, stdio: 'ignore' }, (err) => {
    if (err) console.error('airship-discover error:', err);
  });
  child.unref();
  res.json({ triggered: true });
});

// Optional util endpoint: list repo files (for training scripts / debugging)
// GET /api/discover/repo-files?repo=...
router.get('/repo-files', async (req, res) => {
  const repo = req.query.repo;
  if (!repo) return res.status(400).json({ error: 'repo required' });
  try {
    const files = await listRepoFiles(repo, true);
    res.json({ repo, files });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Optional util endpoint: download single file via CDN (for training scripts)
// GET /api/discover/cdn-proxy?repo=...&path=...
router.get('/cdn-proxy', async (req, res) => {
  const { repo, path: filePath } = req.query;
  if (!repo || !filePath) return res.status(400).json({ error: 'repo and path required' });
  try {
    const buf = await downloadFromCdn(repo, filePath);
    // stream to client
    res.set('Content-Type', 'application/octet-stream');
    res.send(buf);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
```

Mount in `arkship/src/app.js`:

```js
const discoverRoutes = require('./routes/discover');
app.use('/api/discover', discoverRoutes);
app.use('/discover/manifest', express.static('/var/airship/discover/manifest'));
```

---

## 2) Frontend: Discover panel (React)

File: `arkship/src/pages/Discover.jsx`

```jsx
import { useEffect, useState } from 'react';
import './Discover.css';

function formatTS(ts) {
  if (!ts) return '-';
  try { return new Date(ts).toLocaleString(); }
  catch { return String(ts); }
}

export default function DiscoverPage() {
  const [data, setData] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  async function fetchStatus() {
    try {
      const res = await fetch('/api/discover/status');
      const json = await res.json();
      setData(json);
    } catch (err) {
      setError(err.message);
    }
  }

  async function runDiscover() {
    setRunning(true);
    setError(null);
    try {
      await fetch('/api/discover/run', { method: 'POST' });
      // brief pause then refresh
      await new Promise((r) => setTimeout(r, 800));
      await fetchStatus();
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  }

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 15000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="discover-page">
      <header className="discover-header">
        <h1>Discover</h1>
        <button
          className="run-btn"
          onClick={runDiscover}
          disabled={running}
        >
          {running ? 'Running...' : 'Run Discover'}
        </button>
      </header>

      {error && <div className="alert error">{error}</div>}

      <section className="status-card">
        <h2>Status</h2>
        <p>Last run: {data?.status?.lastRun ? formatTS(data.status.lastRun) : '—'}</p>
        <p>State: {data?.status?.status || '
