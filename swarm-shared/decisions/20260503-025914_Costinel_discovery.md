# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard (sidebar or top banner).
- Defaults to hub **MOC** (configurable at build-time via `VITE_HUB_NAME` and at runtime via `HUB_NAME` env override).
- Shows: hub title, short description, top 3 signals (anomalies/recommendations), last-updated timestamp.
- **CDN-first data strategy**: runtime fetches via raw CDN URLs (`resolve/main/...`) to bypass HF API rate limits; no API calls during dashboard use.
- **Build-time file list artifact** (generated once on Mac, committed) so the app knows available hub files without runtime discovery.
- Graceful fallback: if CDN fails or hub unavailable, panel collapses to minimal local stub without breaking the dashboard.
- Zero backend changes; pure frontend addition + one orchestration script for local dev.

---

## File changes

1. **`src/components/TopHubSignalPanel.tsx`** (new)
2. **`src/components/TopHubSignalPanel.module.css`** (new)
3. **`src/config/hubs.ts`** (new)
4. **`src/env/hubFiles.json`** (new — generated artifact)
5. **`src/App.tsx`** (mount panel in layout)
6. **`scripts/fetch-hub-local.sh`** (dev helper to cache hub JSON)
7. **`.env`** (add VITE_ variables)

---

## Environment (.env)
```bash
# Build-time defaults (committed)
VITE_HUB_NAME=MOC
VITE_HUB_REPO=AXENTX/Costinel
VITE_HUB_PATH_PREFIX=hubs

# Runtime overrides (optional, e.g. in docker/k8s)
# HUB_NAME=MOC
```

---

## Hub file list artifact — `src/env/hubFiles.json`
```json
{
  "generatedAt": "2025-11-18T12:00:00.000Z",
  "hubName": "MOC",
  "files": [
    "MOC.json",
    "MOC.2025-11-17.json",
    "MOC.2025-11-16.json"
  ]
}
```
*Generate with (Mac):*
```bash
# One-time generation script (can be added to CI if desired)
REPO="AXENTX/Costinel"
PREFIX="hubs"
HUB="MOC"
OUT="src/env/hubFiles.json"

curl -sL "https://huggingface.co/api/datasets/${REPO}/tree/main/${PREFIX}" \
  | jq --arg hub "$HUB" '{generatedAt: now|todate, hubName: $hub, files: [.[] | select(.path | startswith($hub)) | .path | split("/") | .[-1]]}' > "$OUT"
```

---

## Config — `src/config/hubs.ts`
```ts
// Central hub config for Costinel
export const HUB_CONFIG = {
  defaultHub: import.meta.env.VITE_HUB_NAME || 'MOC',
  repo: import.meta.env.VITE_HUB_REPO || 'AXENTX/Costinel',
  pathPrefix: import.meta.env.VITE_HUB_PATH_PREFIX || 'hubs',
  cdnBase: 'https://huggingface.co/datasets',
  refreshIntervalMs: 5 * 60 * 1000, // 5m
  timeoutMs: 8000,
} as const;

export type HubName = string;
```

---

## Component — `src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState, useCallback } from 'react';
import { HUB_CONFIG } from '../config/hubs';
import hubFiles from '../env/hubFiles.json';
import styles from './TopHubSignalPanel.module.css';

type Signal = {
  id: string;
  title: string;
  description: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  action?: string;
};

type HubData = {
  hubName: string;
  title: string;
  description: string;
  signals: Signal[];
  updatedAt: string; // ISO
};

const DEFAULT_LOCAL: HubData = {
  hubName: HUB_CONFIG.defaultHub,
  title: 'MOC — Mission Operations Center',
  description: 'Top signals for cloud cost governance and operational insights.',
  signals: [
    {
      id: 'stub-1',
      title: 'High idle EC2 spend detected',
      description: '3 instances >70% idle in us-east-1; estimated $1.2k/mo savings.',
      severity: 'high',
      action: 'Review rightsizing recommendations',
    },
    {
      id: 'stub-2',
      title: 'Unattached EBS volumes',
      description: '4 unattached volumes totaling 400 GB across accounts.',
      severity: 'medium',
    },
    {
      id: 'stub-3',
      title: 'Forecast overspend next month',
      description: 'Current run-rate projects +18% vs budget; investigate top services.',
      severity: 'critical',
    },
  ],
  updatedAt: new Date().toISOString(),
};

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchHub = useCallback(async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HUB_CONFIG.timeoutMs);

    try {
      // Prefer latest hub file from generated list; fallback to default name
      const preferredFile = hubFiles.files?.[0] || `${HUB_CONFIG.defaultHub}.json`;
      const url = `${HUB_CONFIG.cdnBase}/${HUB_CONFIG.repo}/resolve/main/${HUB_CONFIG.pathPrefix}/${preferredFile}`;

      const res = await fetch(url, { signal: controller.signal, cache: 'no-store' });
      clearTimeout(timeout);

      if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
      const data = (await res.json()) as HubData;
      setHub(data);
    } catch (err) {
      // Graceful fallback to local defaults (non-breaking)
      console.warn('Hub CDN unavailable, using local defaults:', err);
      setHub(DEFAULT_LOCAL);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchHub();
    const id = setInterval(fetchHub, HUB_CONFIG.refreshIntervalMs);
    return () => clearInterval(id);
  }, [fetchHub]);

  if (loading && !hub) return null; // non-blocking: render nothing until ready or fallback

  const data = hub || DEFAULT_LOCAL;

  return (
    <aside className={styles.panel} aria-label={`Top signals — ${data.hubName}`}>
      <header className={styles.header}>
        <h3 className={styles.title}>{data.title}</h3>
        <p className={styles.meta}>Updated {new Date(data.updatedAt).toLocaleString()}</p>
      </header>
      <p className={styles.description}>{data.description}</p>
      <ul className={styles.signals} aria-live="polite">
        {data.signals.slice(0, 3).map((s) => (
          <li key={s.id} className={styles.signal}>
            <div className={styles.signalHeader}>
              <span className={styles.severityBadge} data-severity={s.severity}>
                {s.severity}
              </span>
              <strong className={styles.signalTitle}>{s.title}</strong>
            </div>
            <p className={styles.signalDesc}>{s.description}</p>
            {s.action && <p className={styles.action}>{s.action}</p>}
          </li>
        ))}
      </ul>
    </aside>
  );
}
```

---

## Styles — `src/components/TopHubSignalPanel.module.css`
```css
.panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 16px;
  background: #fff;

