# Costinel / backend

## Final Implementation — CDN-First Top-Hub Signal Panel (<2h)

**Core principle**: zero HF API calls at runtime; CDN-only fetches; non-blocking UI; concrete, copy-paste-ready code.

---

### 1) Architecture (single source of truth)

- **Build/CI (Mac or runner)**  
  - Run **one** `listRepoTree` for `enriched/<date>` → produce `top-hub-filelist.json`  
  - Commit or copy into repo under `public/data/` (or both commit + CDN)  
  - Requires `HF_TOKEN` only at build time

- **Runtime (Costinel backend)**  
  - Serve baked file from `public/data/top-hub-filelist.json` (local)  
  - Expose minimal endpoint `/api/signals/top-hub` (cached, 5m TTL)  
  - If baked file missing → `204` (frontend renders nothing)

- **Frontend**  
  - Fetch `/api/signals/top-hub` asynchronously  
  - On error/204 → render nothing (non-blocking)  
  - Links use CDN URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{file}` (no auth)

---

### 2) Build script — `scripts/bake-top-hub-files.js`

```js
#!/usr/bin/env node
/**
 * Build-time: create top-hub-filelist.json for CDN-first serving.
 * Usage: HF_TOKEN=... node scripts/bake-top-hub-files.js --date=2026-05-03 --out=public/data/top-hub-filelist.json
 * Requires HF_TOKEN only at build time.
 */

import { Command } from 'commander';
import { HfApi } from '@huggingface/hub';
import fs from 'fs/promises';
import path from 'path';

const program = new Command();
program
  .requiredOption('--date <date>', 'Date folder in enriched/ (YYYY-MM-DD)')
  .requiredOption('--out <file>', 'Output JSON path')
  .option('--repo <repo>', 'HF dataset repo', 'AXENTX/Costinel-enriched')
  .parse();

const opts = program.opts();

async function build() {
  const api = new HfApi({ token: process.env.HF_TOKEN });
  const folderPath = `enriched/${opts.date}`;

  // Single list call (non-recursive)
  const tree = await api.listRepoTree({
    repo: opts.repo,
    path: folderPath,
    recursive: false,
  });

  const files = (tree.files || [])
    .filter((f) => /\.(parquet|json|csv)$/i.test(f.path))
    .map((f) => ({
      path: f.path,
      size: f.size,
      lfs: !!f.lfs,
      cdn: `https://huggingface.co/datasets/${opts.repo}/resolve/main/${f.path}`,
    }));

  // Determine top hub by filename prefix heuristics
  const counts = files.reduce((acc, f) => {
    const base = path.basename(f.path);
    const m = base.match(/^(MOC|AWS|GCP|AZURE|CSP|TAG|RI|ANOMALY)-/i);
    const hub = m ? m[1].toUpperCase() : 'OTHER';
    acc[hub] = (acc[hub] || 0) + 1;
    return acc;
  }, {});

  const topHub = Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] || 'MOC';

  // Pick up to 3 contextual files for quick signals
  const signals = files
    .filter((f) => path.basename(f.path).startsWith(topHub))
    .slice(0, 3)
    .map((f) => ({
      title: path.basename(f.path, path.extname(f.path)),
      cdn: f.cdn,
      size: f.size,
    }));

  const payload = {
    generatedAt: new Date().toISOString(),
    date: opts.date,
    repo: opts.repo,
    topHub,
    totalFiles: files.length,
    files,
    signals,
  };

  await fs.mkdir(path.dirname(opts.out), { recursive: true });
  await fs.writeFile(opts.out, JSON.stringify(payload, null, 2), 'utf8');
  console.log(`Wrote ${opts.out} (topHub=${topHub}, files=${files.length})`);
}

build().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

---

### 3) Backend service — `src/backend/services/TopHubSignalService.ts`

```ts
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DATA_PATH = path.join(__dirname, '..', '..', '..', 'public', 'data', 'top-hub-filelist.json');

export interface TopHubPayload {
  topHub: string;
  signals: Array<{ title: string; cdn: string; size: number }>;
  generatedAt: string;
}

let cached: TopHubPayload | null = null;
let cacheAt = 0;
const TTL_MS = 5 * 60 * 1000; // 5 minutes

export async function getTopHubSignal(): Promise<TopHubPayload | null> {
  const now = Date.now();
  if (cached && now - cacheAt < TTL_MS) return cached;

  try {
    const raw = await fs.readFile(DATA_PATH, 'utf8');
    const data = JSON.parse(raw) as TopHubPayload & { files?: unknown[] };
    cached = {
      topHub: data.topHub || 'MOC',
      signals: data.signals || [],
      generatedAt: data.generatedAt,
    };
    cacheAt = now;
    return cached;
  } catch {
    // If baked file missing, return null (non-blocking)
    return null;
  }
}
```

---

### 4) Backend controller — `src/backend/controllers/SignalController.ts`

```ts
import { Request, Response } from 'express';
import { getTopHubSignal } from '../services/TopHubSignalService.js';

export async function getTopHub(req: Request, res: Response) {
  try {
    const payload = await getTopHubSignal();
    if (!payload) {
      // Non-blocking: no baked file available
      return res.status(204).end();
    }
    return res.json(payload);
  } catch (err) {
    // Non-blocking: never crash the page
    console.error('Top-Hub signal error:', err);
    return res.status(204).end();
  }
}
```

---

### 5) Backend route — `src/backend/routes/signalRoutes.ts`

```ts
import express from 'express';
import { getTopHub } from '../controllers/SignalController.js';

const router = express.Router();
router.get('/signals/top-hub', getTopHub);
export default router;
```

Register in your main app:

```ts
import signalRoutes from './backend/routes/signalRoutes.js';
app.use('/api', signalRoutes);
```

---

### 6) Frontend component — `src/components/TopHubSignalPanel.jsx`

```jsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

export default function TopHubSignalPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/api/signals/top-hub')
      .then((r) => {
        if (r.status === 204) return null;
        return r.json();
      })
      .then((payload) => {
        if (mounted && payload) setData(payload);
      })
      .catch(() => {
        // Non-blocking: swallow errors
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false
