# Costinel / quality

## Final Implementation Plan — CDN-First Top-Hub Signal Panel

**Goal**: Add a lightweight, resilient “Top-Hub Signal” panel to Costinel that surfaces the most‑connected hub (e.g., “MOC”) with **zero runtime HF API calls**, using CDN‑first baked data and robust fallbacks. Ships in <2h.

---

### Scope (what ships)
- React component: `TopHubSignalPanel`
- Build script: `scripts/bake-top-hub.js` (runs in CI / pre‑deploy)
- Data contract: `public/data/top-hub.json` (committed + CDN‑accessible, no auth)
- Integration: mount into the existing Quality dashboard route
- Fallbacks: CDN → local committed copy → empty no‑data state (never blocks render)

---

### Architecture (patterns applied)
- **CDN‑first, zero‑auth**: `https://huggingface.co/datasets/AXENTX/knowledge-rag/resolve/main/...` for high‑rate reads.
- **No runtime HF API**: all HF API usage is confined to build time (`list_repo_tree` per folder).
- **Schema minimalism**: `{ hub, score, context, updatedAt, sourceFile }`
- **Resilience**: try CDN → local committed copy → empty; never throw on render.
- **Lightning/quota‑safe**: no training; pure ingestion + UI.

---

### File changes

#### 1) Build script: `scripts/bake-top-hub.js`
```js
#!/usr/bin/env node
/**
 * Bake top-hub insight from knowledge-rag output.
 *
 * Usage:
 *   node scripts/bake-top-hub.js \
 *     --repo "AXENTX/knowledge-rag" \
 *     --path "graph/2026-04-27" \
 *     --out "public/data/top-hub.json"
 *
 * Behavior:
 * - Uses HF API only at build time (list_repo_tree per folder).
 * - Writes minimal { hub, score, context, updatedAt, sourceFile } to out.
 * - If API fails, preserves last-known local file or writes safe empty state.
 */

import { writeFileSync, existsSync, readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { program } from 'commander';
import fetch from 'node-fetch';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

program
  .option('--repo <repo>', 'HF dataset repo', 'AXENTX/knowledge-rag')
  .option('--path <path>', 'Folder path in repo', 'graph/2026-04-27')
  .option('--out <out>', 'Output JSON path (relative to repo root)', 'public/data/top-hub.json')
  .option('--token <token>', 'HF token (optional for higher rate)')
  .parse();

const opts = program.opts();
const HF_API_BASE = 'https://huggingface.co/api';
const HF_CDN_BASE = 'https://huggingface.co/datasets';
const LOCAL_FALLBACK = resolve(__dirname, '..', 'public', 'data', 'top-hub.json');

async function hfFetch(url, headers = {}) {
  const res = await fetch(url, { headers });
  if (res.status === 429) {
    // Respect rate limit: wait 360s as per pattern
    console.warn('HF API 429 — waiting 360s');
    await new Promise((r) => setTimeout(r, 360_000));
    return hfFetch(url, headers);
  }
  if (!res.ok) throw new Error(`HF fetch failed: ${res.status} ${url}`);
  return res.json();
}

async function listFolder(repo, path) {
  // Use recursive=false per folder to avoid pagination explosion
  const url = `${HF_API_BASE}/datasets/${repo}/tree?path=${encodeURIComponent(path)}&recursive=false`;
  const headers = opts.token ? { Authorization: `Bearer ${opts.token}` } : {};
  return hfFetch(url, headers);
}

function pickLatestFile(tree) {
  // Prefer latest date-like folder/file; simple heuristic:
  // - pick first .json or .parquet file sorted by path desc
  const files = tree
    .filter((t) => t.type === 'file' && /\.(json|parquet)$/.test(t.path))
    .sort((a, b) => b.path.localeCompare(a.path));
  return files[0]?.path || null;
}

function parseTopHubFromJson(obj) {
  // Expected shape (knowledge-rag graph extract):
  // { hub: "MOC", score: 0.94, context: "...", sourceFile: "graph/2026-04-27/moc.json" }
  if (!obj || typeof obj !== 'object') return null;
  return {
    hub: String(obj.hub || obj.name || 'Unknown'),
    score: Number(obj.score || obj.centrality || 0),
    context: String(obj.context || obj.summary || ''),
    updatedAt: String(obj.updatedAt || obj.ts || new Date().toISOString()),
    sourceFile: String(obj.sourceFile || obj.path || ''),
  };
}

async function bake() {
  try {
    console.log(`Listing ${opts.repo}/${opts.path} (non-recursive)...`);
    const tree = await listFolder(opts.repo, opts.path);
    const latestFile = pickLatestFile(tree);
    if (!latestFile) throw new Error('No suitable file found in folder');

    // Use CDN URL to fetch raw file content (no auth, high rate)
    const cdnUrl = `${HF_CDN_BASE}/${opts.repo}/resolve/main/${latestFile}`;
    console.log(`Fetching ${cdnUrl}`);
    const res = await fetch(cdnUrl);
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const raw = await res.json();

    const baked = parseTopHubFromJson(raw);
    if (!baked) throw new Error('Could not parse top-hub from file');

    baked.updatedAt = new Date().toISOString();
    baked.sourceFile = latestFile;

    const outPath = resolve(__dirname, '..', opts.out);
    writeFileSync(outPath, JSON.stringify(baked, null, 2), 'utf8');
    console.log(`Baked top-hub to ${outPath}`);
  } catch (err) {
    console.error('Bake failed:', err.message);
    // Preserve existing local fallback if present; otherwise write safe empty state
    if (existsSync(LOCAL_FALLBACK)) {
      console.log('Preserving existing local fallback.');
    } else {
      const empty = { hub: null, score: 0, context: '', updatedAt: new Date().toISOString(), sourceFile: '' };
      writeFileSync(LOCAL_FALLBACK, JSON.stringify(empty, null, 2), 'utf8');
    }
    // Non-blocking for CI
    process.exit(0);
  }
}

bake();
```

Make executable:
```bash
chmod +x scripts/bake-top-hub.js
```

---

#### 2) Data file (committed): `public/data/top-hub.json`
Example (committed by CI after bake):
```json
{
  "hub": "MOC",
  "score": 0.94,
  "context": "Most-connected hub in the knowledge graph; central to cost governance signals and anomaly propagation.",
  "updatedAt": "2026-04-27T14:32:00Z",
  "sourceFile": "graph/2026-04-27/moc.json"
}
```

---

#### 3) React component: `src/components/TopHubSignalPanel.jsx`
```jsx
import { useEffect, useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AlertCircle, RefreshCw } from 'lucide-react';

const CDN_URL =
  'https://huggingface.co/datasets/AXENTX/knowledge-rag/resolve/main/graph/2026-04-27/moc.json';
const LOCAL_URL = '/data/top-hub.json';

export default function TopHubSignalPanel() {
  const [data
