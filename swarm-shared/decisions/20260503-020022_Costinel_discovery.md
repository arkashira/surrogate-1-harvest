# Costinel / discovery

**Final consolidated implementation plan**  
*(Best parts merged; contradictions resolved in favor of correctness + concrete actionability)*

---

## Scope & value
- Frontend-only, read-only React panel that surfaces the most-connected hub (default “MOC”) and its actionable cost-saving proposals from the knowledge graph.
- CDN-first delivery to eliminate HF API rate limits and ensure fast dashboard loads.
- Ship in <2 hours: one new component, one fetch utility, and route integration.

---

## File layout (additions)

```
/opt/axentx/Costinel/
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel/
│   │       ├── TopHubSignalPanel.tsx
│   │       ├── TopHubSignalPanel.module.css
│   │       └── index.ts
│   ├── lib/
│   │   └── cdn.ts
│   └── pages/
│       └── Dashboard.tsx
└── public/
    └── data/
        └── knowledge-graph/
            └── top-hub-moc.json
```

---

## CDN data contract (public JSON)

`public/data/knowledge-graph/top-hub-moc.json`

```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "description": "Central hub for cloud cost governance signals and anomaly triage.",
  "score": 94,
  "proposals": [
    {
      "id": "moc-ri-001",
      "type": "ReservedInstance",
      "title": "Convert 65% of steady-state m5.xlarge to 1-yr No Upfront",
      "impactUsd": 28400,
      "confidence": 0.87,
      "coverage": 0.65,
      "tags": ["AWS", "EC2", "RI"],
      "expiresAt": "2026-06-30T23:59:59Z"
    },
    {
      "id": "moc-snap-002",
      "type": "SnapshotRetention",
      "title": "Delete unattached snapshots older than 45 days (~1.2 TB)",
      "impactUsd": 1860,
      "confidence": 0.92,
      "coverage": 1.0,
      "tags": ["AWS", "EBS"],
      "expiresAt": "2026-05-31T23:59:59Z"
    }
  ],
  "updatedAt": "2026-05-03T04:00:00Z"
}
```

Ops note: produced by `knowledge-rag` post-analysis and placed in `public/data/...` so dashboard fetches hit CDN only (zero HF API calls).

---

## CDN fetch utility (lightweight, no auth)

`src/lib/cdn.ts`

```ts
export interface Proposal {
  id: string;
  type: string;
  title: string;
  impactUsd: number;
  confidence: number;
  coverage: number;
  tags: string[];
  expiresAt: string;
}

export interface TopHubPayload {
  hub: string;
  title: string;
  description: string;
  score: number;
  proposals: Proposal[];
  updatedAt: string;
}

const HUB_PATH = '/data/knowledge-graph/top-hub-moc.json';

export async function fetchTopHub(): Promise<TopHubPayload | null> {
  try {
    const res = await fetch(HUB_PATH, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    return (await res.json()) as TopHubPayload;
  } catch (err) {
    console.error('[TopHubSignalPanel] CDN fetch error:', err);
    return null;
  }
}
```

---

## TopHubSignalPanel component

`src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHub, TopHubPayload, Proposal } from '../../lib/cdn';
import styles from './TopHubSignalPanel.module.css';

function formatUSD(value: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(value);
}

function ProposalRow({ p }: { p: Proposal }) {
  return (
    <div className={styles.proposalRow} key={p.id}>
      <div className={styles.proposalMeta}>
        <span className={styles.proposalType}>{p.type}</span>
        <span className={styles.proposalTags}>{p.tags.map((t) => `#${t}`).join(' ')}</span>
      </div>
      <div className={styles.proposalTitle}>{p.title}</div>
      <div className={styles.proposalFoot}>
        <span className={styles.impact}>Impact: {formatUSD(p.impactUsd)}</span>
        <span className={styles.confidence}>Confidence: {(p.confidence * 100).toFixed(0)}%</span>
      </div>
    </div>
  );
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetchTopHub().then((res) => {
      if (mounted) {
        setData(res);
        setLoading(false);
      }
    });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return <div className={styles.panel}>Loading signals…</div>;
  }

  if (!data) {
    return <div className={styles.panel}>Unable to load top-hub signals.</div>;
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <div>
          <h3 className={styles.hubName}>{data.hub}</h3>
          <p className={styles.hubTitle}>{data.title}</p>
          <p className={styles.hubDesc}>{data.description}</p>
        </div>
        <div className={styles.scoreBadge}>Score: {data.score}</div>
      </div>

      <div className={styles.proposals}>
        {data.proposals.length === 0 ? (
          <div className={styles.empty}>No proposals available.</div>
        ) : (
          data.proposals.map((p) => <ProposalRow p={p} key={p.id} />)
        )}
      </div>

      <div className={styles.footer}>
        <small>Last updated: {new Date(data.updatedAt).toLocaleString()}</small>
      </div>
    </div>
  );
}
```

`src/components/TopHubSignalPanel/TopHubSignalPanel.module.css`

```css
.panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  max-width: 720px;
}

.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.hubName {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.hubTitle {
  margin: 4px 0 0;
  font-size: 0.95rem;
  color: #374151;
}

.hubDesc {
  margin: 4px 0 0;
  font-size: 0.85rem;
  color: #6b7280;
}

.scoreBadge {
  font-weight: 600;
  color: #0ea5e9;
  background: #f0f9ff;
  border: 1px solid #bae6fd;
  border-radius
