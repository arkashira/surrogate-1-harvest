# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal/most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data path; zero backend changes; ships in <2h.

---

### Acceptance Criteria (non-negotiable)
- [ ] Panel appears on dashboard at `/dashboard` labeled **“Top-Hub Signal”**.
- [ ] Default hub = **“MOC”** (configurable via `REACT_APP_TOP_HUB`).
- [ ] Shows hub title, rank, last-updated timestamp, and top 3 proposals (title + 1-line rationale + impact + tags).
- [ ] Data source: CDN path `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub/{hubId}.json` with local fallback `public/data/top-hub/{hubId}.json`.
- [ ] Polished, responsive, accessible (ARIA), keyboard-navigable, matches design tokens.
- [ ] No new backend endpoints; no secrets; no runtime API calls beyond CDN/fetch.

---

### Data Contract (`TopHubPayload`)
```ts
export interface TopHubProposal {
  id: string;
  title: string;
  rationale: string;
  impactScore: number;
  tags: string[];
  href: string;
}

export interface TopHubPayload {
  hubId: string;
  label: string;
  rank: number;
  updatedAt: string; // ISO
  proposals: TopHubProposal[];
}
```

---

### Implementation Tasks (timeboxed)

1. **Add data loader + caching** (`src/lib/top-hub.ts`)  
   - CDN-first fetch with local fallback.  
   - 5-minute stale-while-revalidate cache (memory).  
   - Basic runtime validation (shape + required fields).  
   - Expose `fetchTopHub({ hubId, preferLocal })`.

2. **Create accessible panel component** (`src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`)  
   - Props: `hubId?`, `maxProposals?`, `className?`.  
   - States: loading / error / data.  
   - Render:  
     - Header: label, rank badge, updated timestamp.  
     - Proposal list: title (link), rationale, impact score, tags.  
   - ARIA: `aria-live`, semantic `<section>`, keyboard list navigation.  
   - Responsive: stacks on mobile.

3. **Add module CSS** (`src/components/TopHubSignalPanel/TopHubSignalPanel.module.css`)  
   - Tokens: spacing, type scale, surface, accent.  
   - States: loading skeleton, error, empty.  
   - Print-friendly.

4. **Wire into dashboard** (`src/pages/Dashboard.tsx`)  
   - Insert in top signal zone or right sidebar.  
   - Guard with feature flag `enableTopHubPanel` (env/config).  
   - Default hub from `REACT_APP_TOP_HUB`.

5. **Tests & build verification**  
   - Unit: loader error paths, component render states.  
   - Smoke: `npm run build`, verify CDN path returns valid JSON, no runtime 404s.

---

### Code Snippets (merged best)

#### `src/lib/top-hub.ts`
```ts
export interface TopHubProposal {
  id: string;
  title: string;
  rationale: string;
  impactScore: number;
  tags: string[];
  href: string;
}

export interface TopHubPayload {
  hubId: string;
  label: string;
  rank: number;
  updatedAt: string;
  proposals: TopHubProposal[];
}

const CDN_BASE = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub';
const LOCAL_BASE = '/data/top-hub';

let cache: { payload: TopHubPayload; ts: number } | null = null;
const TTL_MS = 5 * 60 * 1000;

function validatePayload(obj: any): obj is TopHubPayload {
  return (
    obj &&
    typeof obj.hubId === 'string' &&
    typeof obj.label === 'string' &&
    typeof obj.rank === 'number' &&
    typeof obj.updatedAt === 'string' &&
    Array.isArray(obj.proposals) &&
    obj.proposals.every(
      (p: any) =>
        p &&
        typeof p.id === 'string' &&
        typeof p.title === 'string' &&
        typeof p.rationale === 'string' &&
        typeof p.impactScore === 'number' &&
        Array.isArray(p.tags) &&
        typeof p.href === 'string'
    )
  );
}

export async function fetchTopHub(options?: {
  hubId?: string;
  preferLocal?: boolean;
}): Promise<TopHubPayload> {
  const hubId = options?.hubId || process.env.REACT_APP_TOP_HUB || 'MOC';
  const now = Date.now();

  if (cache && cache.payload.hubId === hubId && now - cache.ts < TTL_MS) {
    return cache.payload;
  }

  const urls = options?.preferLocal
    ? [`${LOCAL_BASE}/${hubId}.json`, `${CDN_BASE}/${hubId}.json`]
    : [`${CDN_BASE}/${hubId}.json`, `${LOCAL_BASE}/${hubId}.json`];

  let lastError: Error | undefined;
  for (const url of urls) {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
      const payload = (await res.json()) as TopHubPayload;
      if (!validatePayload(payload)) throw new Error('Invalid payload shape');
      cache = { payload, ts: now };
      return payload;
    } catch (err) {
      lastError = err as Error;
    }
  }

  throw lastError || new Error('Unable to load top-hub data');
}
```

#### `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHub, type TopHubPayload } from '../../lib/top-hub';
import styles from './TopHubSignalPanel.module.css';

interface Props {
  hubId?: string;
  maxProposals?: number;
  className?: string;
}

export const TopHubSignalPanel: React.FC<Props> = ({
  hubId: propHubId,
  maxProposals = 3,
  className = '',
}) => {
  const envHubId = process.env.REACT_APP_TOP_HUB;
  const hubId = propHubId || envHubId || 'MOC';

  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHub({ hubId })
      .then((data) => {
        if (!mounted) return;
        setPayload(data);
        setError(null);
      })
      .catch((err) => {
        if (!mounted) return;
        setError(err.message || 'Failed to load top-hub');
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, [hubId]);

  const proposals = payload ? payload.proposals.slice(0, maxProposals) : [];

  return (
    <section
      className={`${styles.panel} ${className}`}
      aria-label={`Top hub: ${hubId}`}
      aria-live={loading ? 'off' : 'polite'}
    >
      <header className={styles.header}>
        <h2 className={styles.title}>Top-Hub Signal</h2>
        {payload && (
          <>
            <div className={styles.hubMeta}>
              <span className={styles.hubLabel}>{payload.label}</span>
              <span className={styles.rankBadge} aria-label={`Rank ${payload.rank}`}>
                #{
