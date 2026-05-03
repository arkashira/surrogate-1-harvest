# Costinel / quality

## Final Implementation Plan  
**Top-Hub Signal Panel — CDN-first, <2h, telemetry-aware, zero backend changes**

### What we ship
- A **non-blocking Top‑Hub Signal Panel** mounted on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows:
  - Hub title and short description  
  - Top **3 signals** (anomalies / recommendations)  
  - Top **5 connected docs** (title + summary)  
  - “View in Knowledge Graph” link  
  - Last updated timestamp  
- **CDN‑first data strategy**:
  - Build‑time script lists hub files once and writes `src/data/hub-filelist.json`.  
  - Runtime fetches use public CDN URLs only (no Hugging Face API calls during dashboard load).  
  - Graceful fallback to cached placeholder if CDN unavailable (no dashboard breakage).  
- **Telemetry‑aware**:
  - Emits `hub_panel_impression` and `hub_doc_click` (non‑blocking, no PII).  
  - Respects Do‑Not‑Track.  
  - Optional lightweight endpoint for collection (keeps core zero‑backend).  
- **Zero backend changes** — pure frontend + one build‑time script.

---

## File changes

### 1) Config
`src/config/hubs.ts`
```ts
export const HUB_NAME = import.meta.env.VITE_HUB_NAME || 'MOC';
export const HUB_REPO = import.meta.env.VITE_HUB_REPO || 'axentx/knowledge-rag';
export const HUB_PANEL_ENABLED = import.meta.env.VITE_HUB_PANEL_ENABLED !== 'false';
```

---

### 2) Build‑time script (run in CI / pre‑build)
`scripts/build-hub-filelist.ts`
```ts
#!/usr/bin/env tsx
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

const HUB_REPO = process.env.VITE_HUB_REPO || 'axentx/knowledge-rag';
const HUB_NAME = process.env.VITE_HUB_NAME || 'MOC';
const OUT_DIR = join(process.cwd(), 'src/data');
const OUT_FILE = join(OUT_DIR, 'hub-filelist.json');

async function listHubFiles() {
  const treeUrl = `https://huggingface.co/api/datasets/${HUB_REPO}/tree/main/hubs/${HUB_NAME}`;
  const res = await fetch(treeUrl);
  if (!res.ok) throw new Error(`HF tree API failed: ${res.status}`);
  const tree = (await res.json()) as Array<{ path: string; type: 'file' | 'directory' }>;
  const files = tree.filter((t) => t.type === 'file').map((t) => t.path);
  return { hub: HUB_NAME, repo: HUB_REPO, files, generatedAt: new Date().toISOString() };
}

(async () => {
  try {
    if (!existsSync(OUT_DIR)) mkdirSync(OUT_DIR, { recursive: true });
    const listing = await listHubFiles();
    writeFileSync(OUT_FILE, JSON.stringify(listing, null, 2));
    console.log(`Wrote ${listing.files.length} hub files to ${OUT_FILE}`);
  } catch (err) {
    console.error(err);
    // Non-blocking for CI: write minimal placeholder so build continues
    if (!existsSync(OUT_DIR)) mkdirSync(OUT_DIR, { recursive: true });
    writeFileSync(OUT_FILE, JSON.stringify({ hub: HUB_NAME, repo: HUB_REPO, files: [], generatedAt: new Date().toISOString() }));
    process.exit(0);
  }
})();
```

Add to `package.json` scripts:
```json
"scripts": {
  "build:hub-files": "tsx scripts/build-hub-filelist.ts",
  "prebuild": "npm run build:hub-files"
}
```

---

### 3) Panel component
`src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import { HUB_NAME, HUB_PANEL_ENABLED } from '../config/hubs';
import './TopHubSignalPanel.css';

const CDN_BASE = 'https://huggingface.co/datasets';
const HUB_REPO = import.meta.env.VITE_HUB_REPO || 'axentx/knowledge-rag';
const HUB_PATH = `hubs/${HUB_NAME}`;
const INDEX_URL = `${CDN_BASE}/${HUB_REPO}/resolve/main/${HUB_PATH}/_index.json`;

type HubIndex = {
  title: string;
  description: string;
  updated_at: string;
  signals?: Array<{ title: string; summary: string; severity?: string }>;
  docs: Array<{ slug: string; title: string; summary: string; updated_at: string }>;
};

type FileListing = { hub: string; repo: string; files: string[]; generatedAt: string };

export default function TopHubSignalPanel() {
  const [index, setIndex] = useState<HubIndex | null>(null);
  const [fileList, setFileList] = useState<FileListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!HUB_PANEL_ENABLED) {
      setLoading(false);
      return;
    }

    let mounted = true;

    // Lightweight telemetry (non-blocking, DNT-aware)
    function emitTelemetry(event: string, payload: Record<string, unknown>) {
      try {
        if (navigator.doNotTrack === '1' || window.doNotTrack === '1') return;
        const url = '/_telemetry';
        const body = JSON.stringify({ event, payload, ts: Date.now() });
        navigator.sendBeacon?.(url, body);
      } catch {
        // noop
      }
    }

    async function load() {
      try {
        // 1) Load build-time file list (fast, local)
        const listRes = await fetch('/data/hub-filelist.json', { cache: 'no-store' });
        if (listRes.ok) {
          const list = (await listRes.json()) as FileListing;
          if (mounted) setFileList(list);
        }

        // 2) Load hub index from CDN (no HF API)
        const idxRes = await fetch(INDEX_URL, { cache: 'no-store' });
        if (!idxRes.ok) throw new Error(`CDN index ${idxRes.status}`);
        const idx = (await idxRes.json()) as HubIndex;

        if (mounted) {
          setIndex(idx);
          emitTelemetry('hub_panel_impression', {
            hub: HUB_NAME,
            doc_count: idx.docs?.length ?? 0,
            signal_count: idx.signals?.length ?? 0,
          });
        }
      } catch (err: any) {
        if (mounted) setError(err.message);
      } finally {
        if (mounted) setLoading(false);
      }
    }

    load();
    return () => {
      mounted = false;
    };
  }, []);

  if (!HUB_PANEL_ENABLED) return null;
  if (loading) {
    return (
      <div className="hub-panel loading">
        <div className="hub-panel__skeleton"></div>
      </div>
    );
  }

  if (error || !index) {
    return (
      <div className="hub-panel fallback">
        <div className="hub-panel__title">{HUB_NAME}</div>
        <div className="hub-panel__muted">Signals unavailable — using cached insights.</div>
      </div>
    );
  }

  function emitTelemetry(event: string, payload: Record<string, unknown>) {
    try {
      if (navigator.doNotTrack === '1' || window.doNotTrack === '1') return;
      navigator.sendBeacon?.('/_telemetry', JSON.stringify({ event, payload, ts: Date.now() }));
    } catch {
      // noop
    }
  }

  return (
    <aside className="hub-panel" aria-label={`Top hub: ${index.title}`}>
      <header className="hub-panel__header">
        <h3 className="hub-panel__title">{index.title}</h3>

