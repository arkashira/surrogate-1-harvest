# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
- Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- CDN-first data: a single public JSON file (committed by backend/ingestion) is served via CDN. The UI fetches it with zero API/auth and avoids backend rate limits.  
- Incremental, non-breaking, ships in <2h.

---

### Acceptance Criteria
- [ ] Dashboard route renders a responsive panel (mounted on `/dashboard`).  
- [ ] Shows hub name, description, signal score, and top 3 actionable proposals (title, summary/context, impact, effort, tags).  
- [ ] Skeleton, empty, and error states.  
- [ ] CDN fetch with cache-bust on mount (`?t=YYYYMMDDHHmm`) and 5-minute stale-while-revalidate behavior.  
- [ ] No auth headers required; all data is public/static JSON via CDN.

---

### File Changes (frontend)

- `src/pages/Dashboard/Dashboard.tsx` — import and mount panel.  
- `src/pages/Dashboard/TopHubPanel.tsx` — new component.  
- `src/services/topHubService.ts` — CDN fetcher with SWR-like behavior and cache-bust.  
- `src/types/topHub.ts` — lightweight types.  
- `public/data/top-hub/MOC.json` — seed data (committed by ingestion).

---

### Types (`src/types/topHub.ts`)

```ts
export type ProposalImpact = 'High' | 'Medium' | 'Low';
export type ProposalEffort = 'High' | 'Medium' | 'Low';

export interface Proposal {
  id: string;
  title: string;
  summary?: string;
  context?: string;
  impact: ProposalImpact;
  effort: ProposalEffort;
  tags: string[];
}

export interface TopHub {
  hubSlug: string;
  title: string;
  description: string;
  signalScore: number;
  proposals: Proposal[];
}
```

---

### CDN Service (`src/services/topHubService.ts`)

```ts
const CDN_BASE = '/data/top-hub';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

let cached: { data: any; fetchedAt: number } | null = null;

function cacheBust(): string {
  const now = new Date();
  const ts =
    now.getUTCFullYear() +
    String(now.getUTCMonth() + 1).padStart(2, '0') +
    String(now.getUTCDate()).padStart(2, '0') +
    String(now.getUTCHours()).padStart(2, '0') +
    String(now.getUTCMinutes()).padStart(2, '0');
  return `t=${ts}`;
}

export async function fetchTopHub(
  hubSlug = 'MOC',
  { bustCache = true }: { bustCache?: boolean } = {}
): Promise<TopHub | null> {
  // Serve from cache if fresh
  if (cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS) {
    return cached.data as TopHub;
  }

  const url = `${CDN_BASE}/${hubSlug}.json` + (bustCache ? `?${cacheBust()}` : '');
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Failed to load hub: ${res.status}`);
    const data = await res.json();
    cached = { data, fetchedAt: Date.now() };
    return data as TopHub;
  } catch {
    // On failure, return stale cache if available
    if (cached) return cached.data as TopHub;
    return null;
  }
}
```

---

### Component (`src/pages/Dashboard/TopHubPanel.tsx`)

```tsx
import React, { useEffect, useMemo, useState } from 'react';
import { fetchTopHub } from '../../services/topHubService';
import { TopHub, Proposal } from '../../types/topHub';
import styles from './TopHubPanel.module.css';

type Filter = {
  impact?: 'High' | 'Medium' | 'Low' | null;
  effort?: 'High' | 'Medium' | 'Low' | null;
};

const TopHubPanel: React.FC<{ hubSlug?: string }> = ({ hubSlug = 'MOC' }) => {
  const [hub, setHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>({ impact: null, effort: null });

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub(hubSlug, { bustCache: true })
      .then((data) => {
        if (!mounted) return;
        if (data) {
          setHub(data);
          setError(null);
        } else {
          setError('Unable to load hub signals.');
          setHub(null);
        }
      })
      .catch(() => {
        if (!mounted) return;
        setError('Unable to load hub signals.');
        setHub(null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [hubSlug]);

  const filteredProposals = useMemo(() => {
    if (!hub) return [];
    return hub.proposals.filter((p) => {
      if (filter.impact && p.impact !== filter.impact) return false;
      if (filter.effort && p.effort !== filter.effort) return false;
      return true;
    });
  }, [hub, filter]);

  const toggleImpact = (value: Filter['impact']) =>
    setFilter((f) => ({ ...f, impact: f.impact === value ? null : value }));
  const toggleEffort = (value: Filter['effort']) =>
    setFilter((f) => ({ ...f, effort: f.effort === value ? null : value }));

  if (loading) {
    return (
      <aside className={styles.panel} aria-busy="true">
        <div className={styles.skeletonHeader} />
        <div className={styles.skeletonScore} />
        <div className={styles.skeletonList}>
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className={styles.skeletonItem} />
          ))}
        </div>
      </aside>
    );
  }

  if (error || !hub) {
    return (
      <aside className={styles.panel} role="region" aria-label="Top hub signal (unavailable)">
        <p className={styles.error}>{error || 'Unable to load hub signals.'}</p>
      </aside>
    );
  }

  const topThree = filteredProposals.slice(0, 3);

  return (
    <aside className={styles.panel} role="region" aria-label={`Top hub: ${hub.title}`}>
      <header className={styles.header}>
        <div>
          <h2 className={styles.title}>{hub.title}</h2>
          <p className={styles.description}>{hub.description}</p>
        </div>
        <div className={styles.score} title="Signal score">
          {hub.signalScore}
        </div>
      </header>

      <div className={styles.filters} role="group" aria-label="Proposal filters">
        <span className={styles.filterLabel}>Impact</span>
        {(['High', 'Medium', 'Low'] as const).map((v) => (
          <button
            key={v}
            type="button"
            className={filter.impact === v ? styles.filterActive : styles.filter}
            onClick={() => toggleImpact(v)}
            aria-pressed={filter.impact === v}
          >
            {v}
          </button>
        ))}
        <span className={styles.filterLabel} style={{ marginLeft: 12 }}>
          Effort
        </span>
        {(['High',
