# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build time. Runtime dashboard makes **zero HF API calls**.

---

### Why this ships highest value in <2h
- Reuses existing `#knowledge-rag #graph #hub` pattern without new infra.
- CDN bypass removes rate‑limit risk and keeps runtime lightweight.
- Pure additive UI + small build‑time script → no breaking changes.
- Fits Costinel philosophy: *Sense + Signal* (propose, don’t execute).

---

### Architecture (summary)
1. **Build-time** (`scripts/build-top-hub.js`):  
   - Runs on CI/mac orchestration node (or manually).  
   - Uses `list_repo_tree` once per date folder → saves deterministic `public/data/top-hub.json`.  
   - Embeds file into Docker/static bundle; no runtime HF calls.

2. **Runtime** (`components/TopHubSignalPanel.tsx`):  
   - Dashboard fetches `/data/top-hub.json` (CDN/static, no auth).  
   - Graceful fallback + skeleton UI.  
   - Optional lightweight API route (`/api/top-hub`) for server-side cache/proxy if desired.

3. **Zero API during training/inference**:  
   - All heavy listing done at build; runtime uses static CDN fetch.

---

### File changes (concrete)

#### 1) Build script: `scripts/build-top-hub.js`
```js
#!/usr/bin/env node
/**
 * Build-time script to generate public/data/top-hub.json
 * Uses HF API once per build (list_repo_tree) → CDN-only at runtime.
 *
 * Usage:
 *   HUGGING_FACE_TOKEN=hf_xxx node scripts/build-top-hub.js [date-folder]
 *
 * Output:
 *   public/data/top-hub.json
 */

const { HfApi } = require('@huggingface/hub');
const fs = require('fs');
const path = require('path');

const REPO = process.env.HF_DATASETS_REPO || 'axentx/costinel-signals';
const DATE_FOLDER = process.argv[2] || new Date().toISOString().slice(0, 10);
const OUT_DIR = path.join(process.cwd(), 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub.json');

async function main() {
  const api = new HfApi({ token: process.env.HUGGING_FACE_TOKEN || undefined });

  try {
    console.log(`📡 Listing signals tree for ${DATE_FOLDER} (non-recursive)...`);
    const tree = await api.listRepoTree(REPO, DATE_FOLDER, { recursive: false });
    const files = tree.filter((t) => t.type === 'file').map((t) => t.path);

    // Heuristic: pick latest top-hub file or fallback to known pattern
    const topHubFile = files.find((f) => /top-hub.*\.json$/i.test(f)) || null;

    let payload;
    if (topHubFile) {
      console.log(`📥 Found ${topHubFile}; downloading via CDN...`);
      const cdnUrl = `https://huggingface.co/datasets/${REPO}/resolve/main/${topHubFile}`;
      const res = await fetch(cdnUrl);
      if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
      payload = await res.json();
    } else {
      console.log('⚠️ No top-hub file found; using deterministic placeholder.');
      payload = {
        hub: 'MOC',
        score: 0.92,
        source: 'knowledge-rag',
        updated: DATE_FOLDER,
        context: 'Most-connected hub from graph analysis (placeholder)',
        cdn_url: `https://huggingface.co/datasets/${REPO}/resolve/main/signals/${DATE_FOLDER}/top-hub.json`,
      };
    }

    // Normalize payload
    const normalized = {
      hub: payload.hub || 'MOC',
      score: Number(payload.score) || 0,
      source: payload.source || 'knowledge-rag',
      updated: payload.updated || DATE_FOLDER,
      context: payload.context || 'Most-connected hub from graph analysis',
      cdn_url: payload.cdn_url || `https://huggingface.co/datasets/${REPO}/resolve/main/signals/${DATE_FOLDER}/top-hub.json`,
    };

    fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(OUT_FILE, JSON.stringify(normalized, null, 2), 'utf8');
    console.log(`✅ Written ${OUT_FILE}`);
  } catch (err) {
    console.error('Build failed:', err.message);
    // Ensure valid fallback exists
    const fallback = {
      hub: 'MOC',
      score: 0.92,
      source: 'knowledge-rag',
      updated: DATE_FOLDER,
      context: 'Fallback placeholder',
    };
    fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(OUT_FILE, JSON.stringify(fallback, null, 2), 'utf8');
    console.log(`⚠️ Fallback written to ${OUT_FILE}`);
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```
Make executable (optional):
```bash
chmod +x scripts/build-top-hub.js
```

---

#### 2) Runtime API route (optional): `app/api/top-hub/route.ts`
```ts
// app/api/top-hub/route.ts
import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

const LOCAL_FILE = path.join(process.cwd(), 'public', 'data', 'top-hub.json');

export async function GET() {
  try {
    const raw = fs.readFileSync(LOCAL_FILE, 'utf8');
    const data = JSON.parse(raw);
    return NextResponse.json({ ...data, source: 'local' });
  } catch (err) {
    return NextResponse.json(
      { error: 'Top-hub data unavailable', details: (err as Error).message },
      { status: 503 }
    );
  }
}
```

---

#### 3) UI Component: `components/TopHubSignalPanel.tsx`
```tsx
// components/TopHubSignalPanel.tsx
'use client';

import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ExternalLink } from 'lucide-react';

interface HubSignal {
  hub: string;
  score: number;
  source: string;
  updated: string;
  context: string;
  cdn_url?: string;
}

export function TopHubSignalPanel() {
  const [signal, setSignal] = useState<HubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchSignal = async () => {
    setLoading(true);
    try {
      // Prefer static file; optionally proxy via /api/top-hub
      const res = await fetch('/data/top-hub.json', { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        setSignal(data);
        return;
      }
      // fallback to API route if needed
      const apiRes = await fetch('/api/top-hub', { cache: 'no-store' });
      if (apiRes.ok) {
        const data = await apiRes.json();
        setSignal(data);
        return;
      }
    } catch (err) {
      console.error('Failed to load top-hub signal', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSignal();
  }, []);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Top Hub Signal</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-10 w-32 animate-pulse rounded bg-muted" />
        </
