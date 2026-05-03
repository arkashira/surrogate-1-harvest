# Costinel / discovery

### Final consolidated implementation (best of both proposals)

**Scope & constraints**  
- Pure frontend, read-only (Sense + Signal), ≤2h.  
- No backend/API/auth/infra changes.  
- Reuse existing knowledge-rag/graph patterns and top-hub-first guidance.  
- Graceful fallback to static guidance when live data unavailable.  
- Links deeper into existing Costinel recommendations pages (no new routes).

---

### What will ship
A single reusable card that:
1. Shows the most-connected hub (e.g., “MOC”) as primary signal.  
2. Shows top 3 related docs/insights from knowledge-rag/graph.  
3. Shows last-refresh timestamp and a confidence indicator.  
4. Falls back to static guidance when no live data.  
5. Is accessible, performant, and styled consistently.

---

### File changes
- `src/components/costinel/TopHubSignalCard.tsx` (new)  
- `src/components/costinel/TopHubSignalCard.module.css` (new)  
- `src/components/costinel/types.ts` (new)  
- `src/components/costinel/index.ts` (export addition)  
- `src/pages/Dashboard.tsx` (import + mount in sidebar/insights panel)

---

### Types (`src/components/costinel/types.ts`)
```ts
export interface HubNode {
  key: string;
  label: string;
  centrality: number;
  description?: string;
}

export interface RelatedDoc {
  slug: string;
  title: string;
  score: number;
}
```

---

### Component (`src/components/costinel/TopHubSignalCard.tsx`)
```tsx
import React, { useEffect, useState, useCallback } from 'react';
import type { HubNode, RelatedDoc } from './types';
import styles from './TopHubSignalCard.module.css';

const FALLBACK_HUB: HubNode = {
  key: 'MOC',
  label: 'MOC (Meeting of Cost)',
  centrality: 0.92,
  description: 'Cross-project cost governance and decision workflows',
};

const FALLBACK_DOCS: RelatedDoc[] = [
  { slug: 'costinel/ri-coverage-analysis', title: 'RI Coverage Analysis', score: 0.88 },
  { slug: 'costinel/anomaly-detection', title: 'Anomaly Detection Patterns', score: 0.81 },
  { slug: 'costinel/forecasting-methods', title: 'Forecasting Methods', score: 0.76 },
];

const TopHubSignalCard: React.FC = () => {
  const [hub, setHub] = useState<HubNode | null>(null);
  const [docs, setDocs] = useState<RelatedDoc[]>([]);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);

  const fetchTopHub = useCallback(async () => {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2500);

      const res = await fetch('/api/knowledge-rag/top-hub', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      clearTimeout(timeout);

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();

      if (payload?.hub && Array.isArray(payload?.related)) {
        setHub(payload.hub);
        setDocs(payload.related.slice(0, 3));
        setLastRefresh(payload.ts || new Date().toISOString());
        return;
      }
      throw new Error('Invalid payload');
    } catch {
      // Graceful fallback (read-only)
      setHub(FALLBACK_HUB);
      setDocs(FALLBACK_DOCS);
      setLastRefresh(new Date().toISOString());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    if (mounted) void fetchTopHub();
    return () => {
      mounted = false;
    };
  }, [fetchTopHub]);

  if (loading) {
    return (
      <aside className={styles.card} aria-busy="true">
        <div className={styles.header}>
          <span className={styles.title}>Top hub signal</span>
          <span className={styles.badge}>{'\u2026'}</span>
        </div>
        <div className={styles.loadingRows}>
          <div className={styles.row} />
          <div className={styles.row} />
          <div className={styles.row} />
        </div>
      </aside>
    );
  }

  if (!hub) return null;

  const confidence = Math.min(100, Math.round((hub.centrality || 0.85) * 100));
  const confidenceColor =
    confidence >= 85 ? 'var(--signal-high)' : confidence >= 60 ? 'var(--signal-medium)' : 'var(--signal-low)';

  const formatTime = (iso: string) => {
    try {
      return new Intl.DateTimeFormat(undefined, {
        hour: 'numeric',
        minute: 'numeric',
        month: 'short',
        day: 'numeric',
      }).format(new Date(iso));
    } catch {
      return iso;
    }
  };

  return (
    <aside className={styles.card} aria-label="Top hub signal">
      <div className={styles.header}>
        <span className={styles.title}>Top hub signal</span>
        <span className={styles.badge} style={{ backgroundColor: confidenceColor }}>
          {confidence}%
        </span>
      </div>

      <div className={styles.hub}>
        <span className={styles.hubKey}>{hub.key}</span>
        <span className={styles.hubLabel}>{hub.label}</span>
      </div>

      {hub.description && <p className={styles.description}>{hub.description}</p>}

      <section className={styles.related} aria-label="Related insights">
        <h4 className={styles.sectionTitle}>Related insights</h4>
        <ul className={styles.list}>
          {docs.map((d) => (
            <li key={d.slug}>
              <a
                href={`/recommendations/${d.slug}`}
                className={styles.link}
                target="_self"
                rel="noopener"
              >
                {d.title}
              </a>
            </li>
          ))}
        </ul>
      </section>

      <footer className={styles.footer}>
        <small className={styles.meta}>
          Updated {lastRefresh ? formatTime(lastRefresh) : '—'}
        </small>
      </footer>
    </aside>
  );
};

export default TopHubSignalCard;
```

---

### Styles (`src/components/costinel/TopHubSignalCard.module.css`)
```css
.card {
  --signal-high: #10b981;
  --signal-medium: #f59e0b;
  --signal-low: #ef4444;
  --muted: #6b7280;
  --accent: #2563eb;

  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 1rem;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #e6e9ee;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
  color: #111827;
  max-width: 320px;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.title {
  font-weight: 600;
  font-size: 0.875rem;
}

.badge {
  font-size: 0.75rem;
  padding: 0.
