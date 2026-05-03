# Costinel / quality

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, telemetry-aware)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, **connection strength**, top 5 signals (anomalies/recommendations), and a **last-updated timestamp**.
- **CDN-first data fetch**: primary source is a single hub metadata file at  
  `https://huggingface.co/datasets/{repo}/resolve/main/hubs/{hub}.json`  
  (bypasses HF API rate limits). A **build-time file list** is embedded to enable fallback/discovery when needed.
- **Telemetry-aware**: respects `window.axentxTelemetry` opt-out and emits minimal, privacy-safe events (`hub_signal_impression`).
- **Graceful degradation**: if CDN fails or panel is disabled, collapses to a small info chip; never breaks the dashboard.
- **Zero backend changes** — pure frontend addition (React + Tailwind) + one small Node helper for build-time hub file list.

---

## File changes (minimal, safe)

1. `src/components/TopHubSignalPanel.tsx` — new React component.
2. `scripts/build-embed-filelist.js` — one-time script to produce `src/embedded/hub-filelist.json`.
3. `src/embedded/hub-filelist.json` — generated (committed) file list for the hub folder.
4. `src/config/hubs.ts` — hub metadata (title, description, repo, path, CDN URLs).
5. `src/hooks/useCdnHub.ts` — hook to load hub JSON from CDN and optionally use file list as fallback.
6. `src/types/telemetry.ts` — lightweight telemetry types (opt-out respected).
7. Dashboard integration: mount panel in `src/pages/Dashboard.tsx` (non-blocking, top-right card).

---

## Code snippets (merged + corrected)

### 1) Hub config (`src/config/hubs.ts`)
```ts
export interface HubConfig {
  name: string;
  title: string;
  description: string;
  repo: string; // huggingface repo (datasets)
  path: string; // folder containing hub files
  cdnPrefix: string; // base CDN URL pattern for files
  metadataUrl: string; // single JSON metadata for CDN-first fetch
}

export const HUBS: Record<string, HubConfig> = {
  MOC: {
    name: 'MOC',
    title: 'MOC — Method & Operations Catalog',
    description:
      'Central hub for operational playbooks, anomaly patterns, and cost-signal methods used by Costinel.',
    repo: 'axentx/costinel-hub',
    path: 'moc',
    cdnPrefix: 'https://huggingface.co/datasets/axentx/costinel-hub/resolve/main/moc',
    metadataUrl:
      'https://huggingface.co/datasets/axentx/costinel-hub/resolve/main/hubs/moc.json',
  },
};
```

### 2) Build script to embed file list (`scripts/build-embed-filelist.js`)
```js
#!/usr/bin/env node
/**
 * Pre-list hub folder once and embed as JSON to avoid runtime API calls.
 * Run during CI/build. Uses HF API only once per build.
 *
 * Usage: node scripts/build-embed-filelist.js MOC
 */
const fs = require('fs');
const path = require('path');
const { HfApi } = require('@huggingface/hub');

const HUB_NAME = process.argv[2] || 'MOC';
const { HUBS } = require('../src/config/hubs');
const cfg = HUBS[HUB_NAME];
if (!cfg) {
  console.error(`Unknown hub: ${HUB_NAME}`);
  process.exit(1);
}

async function run() {
  const api = new HfApi();
  // non-recursive list to minimize requests
  const tree = await api.listRepoTree(cfg.repo, cfg.path, { recursive: false });
  const files = (tree.files || []).filter((f) => f.path.endsWith('.json')).map((f) => f.path);

  const outDir = path.join(__dirname, '..', 'src', 'embedded');
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

  const outFile = path.join(outDir, `${cfg.name.toLowerCase()}-filelist.json`);
  fs.writeFileSync(
    outFile,
    JSON.stringify({ hub: cfg.name, generatedAt: new Date().toISOString(), files }, null, 2)
  );
  console.log(`Embedded file list written to ${outFile} (${files.length} files)`);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

### 3) CDN fetch hook (`src/hooks/useCdnHub.ts`)
```ts
import { useEffect, useState } from 'react';
import { HubConfig } from '../config/hubs';

export interface HubSignal {
  id: string;
  title: string;
  summary: string;
  severity: 'low' | 'medium' | 'high' | 'info';
  ts: string;
  url?: string;
}

export interface HubMetadata {
  hub: string;
  connectionStrength?: number; // 0-100
  lastUpdated?: string;
  signals?: HubSignal[];
}

export function useCdnHub(cfg: HubConfig, fallbackFileList?: string[]) {
  const [metadata, setMetadata] = useState<HubMetadata | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    async function load() {
      setLoading(true);
      try {
        // Primary: CDN-first single metadata file
        const res = await fetch(cfg.metadataUrl, { cache: 'no-store' });
        if (res.ok) {
          const json = await res.json();
          if (mounted) {
            setMetadata({
              hub: json.hub || cfg.name,
              connectionStrength: json.connectionStrength ?? undefined,
              lastUpdated: json.lastUpdated ?? undefined,
              signals: Array.isArray(json.signals) ? json.signals : [],
            });
            setLoading(false);
            return;
          }
        }
      } catch {
        // fallback below
      }

      // Fallback: try to synthesize from embedded file list + CDN files
      if (fallbackFileList?.length) {
        try {
          const results: HubSignal[] = [];
          const recent = [...fallbackFileList].sort().reverse().slice(0, 5);

          await Promise.all(
            recent.map(async (filePath) => {
              try {
                const url = `${cfg.cdnPrefix}/${filePath.split('/').pop()}`;
                const res = await fetch(url, { cache: 'no-store' });
                if (!res.ok) return;
                const json = await res.json();
                if (json && json.id) {
                  results.push({
                    id: json.id,
                    title: json.title || json.id,
                    summary: json.summary || '',
                    severity: json.severity || 'info',
                    ts: json.ts || '',
                    url: json.url,
                  });
                }
              } catch {
                // ignore individual failures
              }
            })
          );

          if (mounted) {
            setMetadata({
              hub: cfg.name,
              signals: results,
              lastUpdated: new Date().toISOString(),
            });
          }
        } catch {
          // ignore
        }
      }

      if (mounted) {
        setLoading(false);
      }
    }

    load();
    return () => {
      mounted = false;
    };
  }, [cfg, fallbackFileList]);

  const signals = metadata?.signals || [];
  return { metadata, signals, loading };
}
```

### 4) Telemetry hook (`src/hooks/useTelemetry.ts`)
```ts
import { useCallback } from 'react';

export function useTelemetry() {
  const emit = useCallback((event: string, payload?: Record<string, unknown>) => {
    if (typeof window === 'undefined') return;
    // Respect opt-out
    if ((window as any).axentxTelemetry === false) return;

    try {

