# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
- Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- CDN-first data: a single `top-hub.json` (published by backend/knowledge-rag) is fetched from `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json` (no Authorization, bypasses HF API rate limits).  
- Incremental: renders immediately if CDN fetch fails (graceful fallback) and exposes refresh/retry.  
- Target effort: ~90–120 min (single component + route + CDN contract).

---

### File changes (high-level)

```
/opt/axentx/Costinel/
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel/
│   │       ├── TopHubSignalPanel.tsx
│   │       ├── TopHubSignalPanel.module.css
│   │       └── index.ts
│   ├── hooks/
│   │   └── useCDNResource.ts
│   ├── pages/
│   │   └── Dashboard/
│   │       └── Dashboard.tsx   (add panel)
│   └── types/
│       └── knowledge.ts
└── package.json (no deps added)
```

---

### 1) Types (`src/types/knowledge.ts`)

```ts
export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  confidence: number; // 0..1
  tags: string[];
  href?: string;
}

export interface TopHubPayload {
  hub: string;
  label: string;
  description: string;
  updatedAt: string; // ISO
  proposals: Proposal[];
  cdnPath: string;
}
```

---

### 2) CDN hook (`src/hooks/useCDNResource.ts`)

```ts
import { useEffect, useState, useCallback } from 'react';

export function useCDNResource<T>(url: string, opts: { refreshInterval?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchResource = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
      const json = (await res.json()) as T;
      setData(json);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    fetchResource();
    if (opts.refreshInterval) {
      const id = setInterval(fetchResource, opts.refreshInterval);
      return () => clearInterval(id);
    }
  }, [fetchResource, opts.refreshInterval]);

  const retry = fetchResource;
  return { data, loading, error, retry };
}
```

---

### 3) Component (`src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`)

```tsx
import React from 'react';
import { TopHubPayload, Proposal } from '../../types/knowledge';
import { useCDNResource } from '../../hooks/useCDNResource';
import styles from './TopHubSignalPanel.module.css';

const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json';

function ImpactBadge({ impact }: { impact: Proposal['impact'] }) {
  return <span className={`${styles.badge} ${styles[impact]}`}>{impact}</span>;
}

function ProposalCard({ p }: { p: Proposal }) {
  return (
    <article className={styles.proposalCard}>
      <div className={styles.proposalHeader}>
        <h4 className={styles.proposalTitle}>{p.title}</h4>
        <ImpactBadge impact={p.impact} />
      </div>
      <p className={styles.proposalSummary}>{p.summary}</p>
      <div className={styles.proposalMeta}>
        <span className={styles.confidence}>Confidence: {(p.confidence * 100).toFixed(0)}%</span>
        {p.tags?.length ? (
          <div className={styles.tags}>
            {p.tags.map((t) => (
              <span key={t} className={styles.tag}>
                {t}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {p.href ? (
        <a className={styles.proposalLink} href={p.href} target="_blank" rel="noopener noreferrer">
          View details →
        </a>
      ) : null}
    </article>
  );
}

export default function TopHubSignalPanel() {
  const { data, loading, error, retry } = useCDNResource<TopHubPayload>(CDN_URL, {
    refreshInterval: 5 * 60 * 1000,
  });

  if (loading && !data) {
    return (
      <section className={styles.panel}>
        <div className={styles.loading}>Loading top-hub signals…</div>
      </section>
    );
  }

  const hub = data || {
    hub: 'MOC',
    label: 'Mission Operations Center',
    description: 'Top operational cost hub (fallback).',
    updatedAt: new Date().toISOString(),
    proposals: [],
    cdnPath: CDN_URL,
  };

  return (
    <section className={styles.panel}>
      <header className={styles.header}>
        <div>
          <h3 className={styles.hubLabel}>{hub.label}</h3>
          <p className={styles.hubDesc}>{hub.description}</p>
        </div>
        <div className={styles.headerMeta}>
          <span className={styles.updated}>
            Updated: {new Date(hub.updatedAt).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}
          </span>
          <button className={styles.retryBtn} onClick={retry} disabled={loading}>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      {error && (
        <div className={styles.error}>
          Could not load latest signals from CDN. Showing cached data.
          <button className={styles.retryBtn} onClick={retry} disabled={loading}>
            Retry
          </button>
        </div>
      )}

      <div className={styles.proposals}>
        {hub.proposals && hub.proposals.length > 0 ? (
          hub.proposals.map((p) => <ProposalCard key={p.id} p={p} />)
        ) : (
          <div className={styles.empty}>No actionable proposals at this time.</div>
        )}
      </div>

      <footer className={styles.footer}>
        <small>
          Signals sourced from knowledge graph (MOC). Costinel = Sense + Signal — ไม่ Execute.
        </small>
      </footer>
    </section>
  );
}
```

---

### 4) Styles (`src/components/TopHubSignalPanel/TopHubSignalPanel.module.css`)

```css
.panel {
  border: 1px solid #e6e9ee;
  border-radius: 10px;
  padding: 18px 20px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(16,24,40,0.04);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}

.hubLabel {
  margin: 0;
  font-size: 18px;
  font-weight: 600;

