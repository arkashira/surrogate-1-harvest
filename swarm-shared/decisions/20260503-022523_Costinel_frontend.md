# Costinel / frontend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data (no HF API at runtime), 100% type-safe, ships in <2h.

---

### 1) Architecture (CDN-first, zero-runtime API)

- **Data source**: `knowledge-rag` produces a small JSON file per date:  
  `public/knowledge-rag/top-hub/{date}/moc.json`  
  (deployed to CDN via CI; frontend fetches via `/knowledge-rag/...` — no auth, no rate limit)
- **Shape** (minimal, projection at build time):
  ```json
  {
    "hub": "MOC",
    "score": 0.94,
    "updatedAt": "2026-04-27T14:30:00Z",
    "proposals": [
      {
        "id": "moc-ri-001",
        "title": "RI coverage gap in us-east-1",
        "signal": 0.92,
        "action": "Purchase 1yr No Upfront for m5.xlarge",
        "savingsUSD": 12400,
        "context": "37 underutilized on-demand instances"
      },
      { "id": "...", "title": "...", "signal": 0.87, "action": "...", "savingsUSD": 8700, "context": "..." },
      { "id": "...", "title": "...", "signal": 0.81, "action": "...", "savingsUSD": 5100, "context": "..." }
    ]
  }
  ```

- **Why this works**:  
  - Follows “pre-list file paths once, embed in training script” pattern — but for frontend: single CDN fetch, zero HF API during runtime.  
  - Keeps frontend simple and cache-friendly (static JSON, long TTL).  
  - Attribution is filename-based (`moc.json`) — no extra `source`/`ts` columns.

---

### 2) File changes

```
Costinel/
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel/
│   │       ├── TopHubSignalPanel.tsx
│   │       ├── TopHubSignalPanel.types.ts
│   │       ├── TopHubSignalPanel.styles.ts
│   │       └── index.ts
│   ├── hooks/
│   │   └── useTopHubSignals.ts
│   └── lib/
│       └── cdn.ts
└── public/
    └── knowledge-rag/
        └── top-hub/
            └── 2026-04-27/
                └── moc.json        # committed by ops (CDN path)
```

---

### 3) Data contract (CDN JSON)

`public/knowledge-rag/top-hub/2026-04-27/moc.json`

```json
{
  "hub": "MOC",
  "score": 0.94,
  "updatedAt": "2026-04-27T14:30:00Z",
  "proposals": [
    {
      "id": "moc-ri-001",
      "title": "RI coverage gap in us-east-1",
      "signal": 0.92,
      "action": "Purchase 1yr No Upfront for m5.xlarge",
      "savingsUSD": 12400,
      "context": "37 underutilized on-demand instances"
    },
    {
      "id": "moc-ebs-002",
      "title": "Delete unattached EBS (30d+)",
      "signal": 0.88,
      "action": "Run nightly cleanup job with 7d retention",
      "savingsUSD": 8700,
      "context": "12 unattached volumes"
    },
    {
      "id": "moc-asg-003",
      "title": "Convert weekend ASG min=0 for batch fleet",
      "signal": 0.81,
      "action": "Update ASG schedule via Terraform",
      "savingsUSD": 5100,
      "context": "Weekend idle capacity"
    }
  ]
}
```

---

### 4) CDN fetch utility (zero-auth, bypass HF API)

`src/lib/cdn.ts`

```ts
const CDN_ROOT = process.env.PUBLIC_URL || '';

export async function fetchTopHubSignals(): Promise<TopHubSignalsResponse> {
  // Use latest date path from CI or fallback to a known date.
  // For MVP, hardcode the latest known date; later replace with index lookup.
  const datePath = '2026-04-27';
  const res = await fetch(`${CDN_ROOT}/knowledge-rag/top-hub/${datePath}/moc.json`, {
    cache: 'no-store',
  });

  if (!res.ok) {
    throw new Error(`CDN fetch failed: ${res.status}`);
  }
  return res.json();
}
```

---

### 5) Hook (server-side render friendly)

`src/hooks/useTopHubSignals.ts`

```ts
import { useQuery } from '@tanstack/react-query';
import { fetchTopHubSignals } from '@/lib/cdn';
import type { TopHubSignalsResponse } from '@/components/TopHubSignalPanel/TopHubSignalPanel.types';

export function useTopHubSignals() {
  return useQuery<TopHubSignalsResponse>({
    queryKey: ['top-hub-signals'],
    queryFn: fetchTopHubSignals,
    staleTime: 1000 * 60 * 10, // 10m
    retry: 1,
  });
}
```

---

### 6) Component

`src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`

```tsx
import React from 'react';
import { useTopHubSignals } from '@/hooks/useTopHubSignals';
import { Card } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Spinner } from '@/components/ui/Spinner';
import { ErrorState } from '@/components/ui/ErrorState';
import { formatUSD } from '@/lib/format';
import type { Proposal } from './TopHubSignalPanel.types';
import styles from './TopHubSignalPanel.styles';

export const TopHubSignalPanel: React.FC = () => {
  const { data, isLoading, error } = useTopHubSignals();

  if (isLoading) return <Spinner centered />;
  if (error || !data) return <ErrorState message="Unable to load signals" />;

  const { hub, proposals } = data;

  return (
    <Card className={styles.panel} aria-label={`${hub} signals`}>
      <header className={styles.header}>
        <div>
          <h3 className={styles.title}>{hub}</h3>
          <p className={styles.meta}>Top hub by signal score</p>
        </div>
        <Badge variant="accent">Top Hub</Badge>
      </header>

      <section className={styles.signals}>
        {proposals.slice(0, 3).map((p) => (
          <ProposalRow key={p.id} proposal={p} />
        ))}
      </section>

      <footer className={styles.footer}>
        <small className={styles.updated}>
          Updated {new Date(data.updatedAt).toLocaleString()}
        </small>
      </footer>
    </Card>
  );
};

function ProposalRow({ proposal }: { proposal: Proposal }) {
  return (
    <div className={styles.row}>
      <div className={styles.rowContent}>
        <span className={styles.rowTitle}>{proposal.title}</span>
        <div className={styles.rowMeta}>
          <Badge variant="soft" size="sm">
            {Math.round(proposal.signal * 100)}% signal
          </
