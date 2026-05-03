# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted in the dashboard top banner (primary) or sidebar (fallback layout).
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short description, **top 3 actionable signals** (anomalies/recommendations), last updated timestamp, and a “View in Knowledge Graph” link.
- **CDN-first data fetch** using HuggingFace `resolve` URLs to bypass API rate limits and auth requirements.
- Graceful, non-blocking fallback to local stub data; never blocks dashboard render.
- Zero backend changes; pure frontend addition.

---

### Why this is highest-value (<2h)
- Applies **#knowledge-rag #graph #hub** pattern (top-hub doc insight) directly.
- Uses **HF CDN bypass** pattern to avoid rate limits during dashboard usage.
- Minimal surface area: one component + one fetch util + one config file.
- Non-blocking, self-contained, and safe to ship independently.

---

### File changes (unified + corrected)

1. `src/config/hubs.ts` — hub config and CDN path builder  
2. `src/lib/fetchHubSignals.ts` — CDN-first fetch with timeout + cache + fallback  
3. `src/types/hub.ts` — minimal shared types  
4. `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx` — UI component  
5. `src/components/TopHubSignalPanel/TopHubSignalPanel.module.css` — minimal styles  
6. Mount in dashboard layout (e.g., `src/pages/Dashboard/Dashboard.tsx`)

> **Correction**: Candidate 1 used a Svelte-centric path and a less specific repo; Candidate 2 used a clearer repo name but omitted timeout/fallback details. We merge the strongest parts: React/TSX structure + robust fetch with timeout + explicit fallback + correct repo path.

---

### Code snippets

#### 1) `src/config/hubs.ts`
```ts
export const HUB_NAME = import.meta.env.VITE_HUB_NAME || 'MOC';
// Use the same dataset repo as Candidate 2 (clear, public)
export const HUBS_REPO = 'AXENTX/Costinel';

export function getHubCdnUrl(hubName: string): string {
  // CDN bypass: public resolve URL, no Authorization header
  return `https://huggingface.co/datasets/${HUBS_REPO}/resolve/main/hubs/${hubName}.json`;
}
```

#### 2) `src/types/hub.ts`
```ts
export interface HubSignal {
  id: string;
  title: string;
  description?: string;
  severity: 'info' | 'warning' | 'critical';
  actionUrl?: string;
}

export interface HubData {
  title: string;
  description: string;
  signals: HubSignal[];
  updatedAt: string;
}
```

#### 3) `src/lib/fetchHubSignals.ts`
```ts
import { getHubCdnUrl } from '../config/hubs';
import type { HubData } from '../types/hub';

const FALLBACK_HUB_DATA: HubData = {
  title: 'MOC',
  description: 'Knowledge hub for cloud cost governance signals.',
  signals: [
    { id: '1', title: 'No recent anomalies', severity: 'info' },
    { id: '2', title: 'Enable idle resource detection', severity: 'warning' },
  ],
  updatedAt: new Date().toISOString(),
};

function timeout(ms: number): Promise<null> {
  return new Promise((resolve) => setTimeout(() => resolve(null), ms));
}

export async function fetchHubSignals(hubName: string): Promise<HubData> {
  const url = getHubCdnUrl(hubName);
  const controller = new AbortController();

  try {
    const fetchPromise = fetch(url, {
      signal: controller.signal,
      cache: 'no-store',
    });

    // Race fetch against 4s timeout (keeps UI responsive)
    const result = await Promise.race([fetchPromise, timeout(4000)]);

    // If timeout or falsy result, fall back
    if (!result || !(result instanceof Response)) {
      return FALLBACK_HUB_DATA;
    }

    const json = await result.json();

    // Basic shape validation
    if (!json || !Array.isArray(json.signals)) {
      return FALLBACK_HUB_DATA;
    }

    return {
      title: json.title || FALLBACK_HUB_DATA.title,
      description: json.description || FALLBACK_HUB_DATA.description,
      signals: json.signals.slice(0, 3),
      updatedAt: json.updatedAt || FALLBACK_HUB_DATA.updatedAt,
    };
  } catch {
    return FALLBACK_HUB_DATA;
  } finally {
    controller.abort();
  }
}
```

#### 4) `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import { fetchHubSignals } from '../../lib/fetchHubSignals';
import { useHubName } from '../../config/hubs';
import type { HubData } from '../../types/hub';
import styles from './TopHubSignalPanel.module.css';

export function TopHubSignalPanel() {
  const hubName = useHubName();
  const [hub, setHub] = useState<HubData | null>(null);

  useEffect(() => {
    let mounted = true;
    fetchHubSignals(hubName).then((data) => {
      if (mounted) setHub(data);
    });
    return () => {
      mounted = false;
    };
  }, [hubName]);

  // Render fallback immediately so dashboard is never blocked
  const data = hub || {
    title: hubName,
    description: 'Loading signals...',
    signals: [
      { id: 'loading', title: 'Loading...', severity: 'info' as const },
    ],
    updatedAt: new Date().toISOString(),
  };

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h3 className={styles.title}>{data.title}</h3>
        <p className={styles.description}>{data.description}</p>
      </div>

      <div className={styles.signals}>
        {data.signals.map((s) => (
          <div key={s.id} className={`${styles.signal} ${styles[`signal--${s.severity}`]}`}>
            <strong>{s.title}</strong>
            {s.description && <span className={styles.muted}>{s.description}</span>}
          </div>
        ))}
      </div>

      <div className={styles.footer}>
        <small className={styles.muted}>
          Updated: {new Date(data.updatedAt).toLocaleString()}
        </small>
        <a href={`/knowledge-graph?hub=${encodeURIComponent(data.title)}`} className={styles.link}>
          View in Knowledge Graph
        </a>
      </div>
    </div>
  );
}
```

#### 5) `src/components/TopHubSignalPanel/TopHubSignalPanel.module.css`
```css
.panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
  max-width: 320px;
}

.header h3 {
  margin: 0 0 4px;
  font-size: 14px;
}

.description {
  margin: 0 0 8px;
  font-size: 12px;
  color: #6b7280;
}

.signals {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}

.signal {
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 13px;
}

.signal--info {
  background: #eff6ff;
  border: 1px solid #dbeafe;
  color: #1e40af;
}

.signal--warning {
  background: #fffbeb;
