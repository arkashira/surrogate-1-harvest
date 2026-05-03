# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Chosen approach:**  
A non-blocking, CDN-first Top-Hub Signal Panel that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Runtime dashboard fetches a static JSON file from CDN (or SSR fallback). Build-time orchestration runs on Mac and produces the baked artifact.

### 1. Architecture (CDN-first)
- **Build-time** (Mac orchestration):
  1. `list_repo_tree` once per date folder → locate latest `top-hub.json`
  2. Download via CDN (`hf_hub_download` uses CDN) → normalize → write to `public/data/top-hub.json`
  3. Commit/copy artifact into repo or deploy with build output
- **Runtime** (dashboard):
  - Fetch `/data/top-hub.json` (static asset) → render panel
  - If fetch fails, SSR fallback reads same file on server and returns minimal payload (keeps panel non-blocking)
  - Zero HF API, zero auth, zero rate-limit risk

### 2. File changes
```
/opt/axentx/Costinel/
├── public/data/top-hub.json          # generated at build/deploy time
├── scripts/build-top-hub.js          # Mac orchestration script (CDN list + write)
├── src/types/top-hub.ts              # types
├── src/lib/topHubSignal.ts           # CDN fetch util + SSR fallback helper
├── src/components/TopHubSignalPanel.tsx  # React panel component
└── src/pages/Dashboard.tsx           # mount panel in sidebar/header
```

### 3. Implementation

#### `scripts/build-top-hub.js`
```js
#!/usr/bin/env node
/**
 * Build-time script (run on Mac).
 * Prerequisites: HF_TOKEN env (if private), @huggingface/hub
 * Usage: node scripts/build-top-hub.js --repo org/knowledge --out public/data/top-hub.json
 */
import { listRepoTree, hf_hub_download } from '@huggingface/hub';
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function buildTopHubSignal({ repo = 'axentx/knowledge', dateFolder = new Date().toISOString().slice(0, 10), out = 'public/data/top-hub.json' } = {}) {
  try {
    // 1) List top-level date folder (non-recursive)
    const tree = await listRepoTree({ repo, path: dateFolder, recursive: false });
    const topHubFile = tree.find((t) => t.path.endsWith('top-hub.json'));
    if (!topHubFile) {
      console.warn(`No top-hub.json in ${dateFolder}. Using fallback.`);
      await writeFallback(out);
      return;
    }

    // 2) Download via CDN (hf_hub_download uses CDN for public files)
    const localPath = await hf_hub_download({
      repo,
      filename: topHubFile.path,
      repo_type: 'dataset',
      local_dir: path.dirname(out),
      local_dir_use_symlinks: false,
    });

    const content = await fs.readFile(localPath, 'utf8');
    const parsed = JSON.parse(content);

    // Normalize to minimal runtime shape
    const signal = {
      hub: parsed.hub || parsed.top_hub || 'MOC',
      score: Number(parsed.score || parsed.connectivity || 0) || 0,
      context: parsed.context || parsed.label || 'Most-connected hub',
      updatedAt: parsed.updated || parsed.updatedAt || dateFolder,
      sourceFile: topHubFile.path,
    };

    await fs.mkdir(path.dirname(out), { recursive: true });
    await fs.writeFile(out, JSON.stringify(signal, null, 2), 'utf8');
    console.log(`Top-hub signal baked to ${out}:`, signal);
  } catch (err) {
    console.error('Failed to build top-hub signal:', err);
    await writeFallback(out);
  }
}

async function writeFallback(out) {
  const fallback = {
    hub: 'MOC',
    score: 0,
    context: 'Most-connected hub',
    updatedAt: new Date().toISOString().slice(0, 10),
    sourceFile: 'fallback',
  };
  await fs.mkdir(path.dirname(out), { recursive: true });
  await fs.writeFile(out, JSON.stringify(fallback, null, 2), 'utf8');
  console.log('Fallback top-hub signal written.');
}

// CLI
if (process.argv[1] === __filename) {
  const args = process.argv.slice(2).reduce((acc, arg) => {
    const [k, v] = arg.replace(/^--/, '').split('=');
    acc[k] = v ?? true;
    return acc;
  }, {});
  buildTopHubSignal(args).catch((err) => {
    console.error(err);
    process.exit(1);
  });
}

export { buildTopHubSignal };
```

#### `src/types/top-hub.ts`
```ts
export interface TopHubSignal {
  hub: string;
  score: number;
  context: string;
  updatedAt: string; // YYYY-MM-DD
  sourceFile: string;
}
```

#### `src/lib/topHubSignal.ts`
```ts
import type { TopHubSignal } from '../types/top-hub';

export async function fetchTopHubSignal(): Promise<TopHubSignal | null> {
  try {
    // CDN-first: static file served from public/
    const res = await fetch('/data/top-hub.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('No top-hub signal');
    const data = (await res.json()) as TopHubSignal;
    return data;
  } catch {
    return null;
  }
}

// SSR fallback helper (reads file on server)
export async function getTopHubSignalForSSR(): Promise<TopHubSignal | null> {
  try {
    // In SSR environments, read from public directory or file system
    // This is intentionally minimal and non-blocking
    const path = await import('path');
    const fs = await import('fs/promises');
    const filePath = path.join(process.cwd(), 'public/data/top-hub.json');
    const content = await fs.readFile(filePath, 'utf8');
    return JSON.parse(content);
  } catch {
    return null;
  }
}
```

#### `src/components/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { TopHubSignal } from '../types/top-hub';
import './TopHubSignalPanel.css';

export const TopHubSignalPanel: React.FC = () => {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first static asset — zero HF API calls at runtime
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error('No top-hub signal');
        return res.json();
      })
      .then((data: TopHubSignal) => {
        setSignal(data);
        setLoading(false);
      })
      .catch(() => {
        // Graceful fallback: render minimal non-blocking state
        setSignal({
          hub: 'MOC',
          score: 0,
          context: 'Most-connected hub',
          updatedAt: new Date().toISOString().slice(0, 10),
          sourceFile: 'fallback',
        });
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading" role="status" aria-label="Loading top hub signal">
        <div className="skeleton" />
      </div>
    );
  }

  if (!signal) return null;

  return (
    <div className="top-hub-panel" role="region" aria-label="Top hub signal">
      <div className="top-hub-header">
        <span className="top-hub-badge">TOP H
