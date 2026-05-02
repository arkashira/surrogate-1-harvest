# Costinel / frontend

## Final Implementation Plan — Costinel Top-Hub Signal (Frontend)

**Chosen scope (highest-value, <2h, read-only):**  
Add a **dashboard-integrated TopHubSignal widget** that consumes `GET /api/v1/cost-anomaly/signal/top-hub` and surfaces the most-connected hub with severity, anomaly count, insight, and quick navigation to details. It is side-effect-free, polls safely, respects design tokens, and avoids layout shift.

**Why this choice (resolved contradictions):**  
- **Embed in Dashboard (not new route)** — fastest user impact and matches Candidate 1’s minimal-overhead approach while keeping option to link to a detail route later.  
- **Use Candidate 1’s shape + Candidate 2’s clarity** — keep `hub`, `severity`, `anomalyCount`, `insight`, `lastUpdated`, `detailsUrl`; add optional `score` and `auditTrailUrl` when the backend provides them (non-breaking).  
- **Polling, not SSE/WebSocket** — simplest to implement, test, and operate within 2h.  
- **Client-only fetch with credentials** — aligns with existing auth; no CDN/auth-bypass indirection unless infra already supports it (avoid speculative changes).  
- **Strong error/loading UX and theme support** — concrete, copy-pastable styles and behavior.

---

### File changes (minimal, focused)

- `src/types/api.ts` — add `TopHubSignalResponse`.
- `src/services/topHubService.ts` — typed fetch + error handling.
- `src/components/TopHubSignal/TopHubSignal.tsx` — component with polling.
- `src/components/TopHubSignal/TopHubSignal.module.css` — theme-aware styles.
- `src/pages/Dashboard/Dashboard.tsx` — mount in header (or sidebar).
- (Optional) `src/pages/SignalTopHubPage.tsx` — future detail page route.

---

### 1) Types (`src/types/api.ts`)

```ts
// Extend existing API types
export interface TopHubSignalResponse {
  hub: string;               // e.g. "MOC"
  severity: 'low' | 'medium' | 'high' | 'critical';
  anomalyCount: number;      // anomalies linked to this hub
  score?: number;            // optional numeric signal score
  insight: string;           // short contextual insight
  lastUpdated: string;       // ISO timestamp
  detailsUrl: string;        // frontend route or external link
  auditTrailUrl?: string;    // optional audit/trail link
}
```

---

### 2) Service (`src/services/topHubService.ts`)

```ts
const ENDPOINT = '/api/v1/cost-anomaly/signal/top-hub';
const POLL_INTERVAL = 30_000; // 30s

export interface TopHubSignalResponse {
  hub: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  anomalyCount: number;
  score?: number;
  insight: string;
  lastUpdated: string;
  detailsUrl: string;
  auditTrailUrl?: string;
}

export async function fetchTopHubSignal(): Promise<TopHubSignalResponse> {
  const res = await fetch(ENDPOINT, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`Failed to fetch top-hub signal: ${res.status} ${text}`);
  }

  return res.json();
}

export { POLL_INTERVAL };
```

---

### 3) Component (`src/components/TopHubSignal/TopHubSignal.tsx`)

```tsx
import React, { useEffect, useState, useCallback } from 'react';
import { fetchTopHubSignal, POLL_INTERVAL } from '../../services/topHubService';
import styles from './TopHubSignal.module.css';

const severityClass = (s: TopHubSignalResponse['severity']) => {
  switch (s) {
    case 'critical':
      return styles.critical;
    case 'high':
      return styles.high;
    case 'medium':
      return styles.medium;
    default:
      return styles.low;
  }
};

const TopHubSignal: React.FC = () => {
  const [data, setData] = useState<TopHubSignalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await fetchTopHubSignal();
      setData(payload);
      setError(null);
    } catch (err: any) {
      setError(err?.message || 'Signal unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_INTERVAL);
    return () => clearInterval(id);
  }, [load]);

  // No layout shift: render stable container
  return (
    <div className={styles.container} title={data?.lastUpdated ? `Last updated: ${new Date(data.lastUpdated).toLocaleString()}` : ''}>
      {loading && !data && <span className={styles.loading}>Loading signal…</span>}
      {error && !data && <span className={styles.error}>Signal unavailable</span>}

      {data && (
        <>
          <div className={styles.badge} data-severity={data.severity}>
            <span className={severityClass(data.severity)}>{data.severity.toUpperCase()}</span>
          </div>
          <div className={styles.body}>
            <div className={styles.title}>
              Top hub: <strong>{data.hub}</strong>
              {typeof data.score === 'number' && <span className={styles.score}> ({data.score.toFixed(1)})</span>}
            </div>
            <div className={styles.meta}>
              {data.anomalyCount} anomaly{data.anomalyCount !== 1 ? 'ies' : ''}
            </div>
            <div className={styles.insight}>{data.insight}</div>
          </div>
          <a
            href={data.detailsUrl}
            className={styles.link}
            target="_blank"
            rel="noopener noreferrer"
          >
            View details →
          </a>
        </>
      )}
    </div>
  );
};

export default TopHubSignal;
```

---

### 4) Styles (`src/components/TopHubSignal/TopHubSignal.module.css`)

```css
.container {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--bg-elevated, #f8fafc);
  color: var(--text-primary, #0f172a);
  font-size: 13px;
  border: 1px solid var(--border-subtle, #e2e8f0);
  min-height: 44px;
}

.badge {
  display: flex;
  align-items: center;
}

.badge span {
  font-size: 10px;
  font-weight: 700;
  padding: 3px 6px;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.low { background: #dbeafe; color: #1e40af; }
.medium { background: #fef3c7; color: #92400e; }
.high { background: #fee2e2; color: #991b1b; }
.critical { background: #7f1d1d; color: #fff; }

.body {
  flex: 1;
  min-width: 0;
}

.title {
  font-weight: 600;
  margin-bottom: 2px;
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.score {
  font-weight: 400;
  font-size: 12px;
  color: var(--text-muted, #64748b);
}

.meta {
  color: var(--text-m
