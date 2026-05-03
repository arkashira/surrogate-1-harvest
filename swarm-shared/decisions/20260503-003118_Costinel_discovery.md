# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (Read-Only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Constraints**: No backend changes; use existing `/api/knowledge-rag/top-hub` (or fallback to static JSON). Ship in <2h.  
**Outcome**: A reusable React card + route + tests + docs.

---

### 1) File Layout (Add/Modify)

```
Costinel/
└── src/
    ├── components/
    │   └── TopHubSignalCard/
    │       ├── TopHubSignalCard.tsx
    │       ├── TopHubSignalCard.test.tsx
    │       └── index.ts
    ├── pages/
    │   └── Insights/
    │       └── KnowledgeHubPage.tsx
    ├── hooks/
    │   └── useTopHub.ts
    ├── types/
    │   └── knowledge-rag.d.ts
    └── utils/
        └── mock/
            └── topHubMock.ts
```

---

### 2) Types (`src/types/knowledge-rag.d.ts`)

```ts
export interface Signal {
  id: string;
  title: string;
  summary: string;
  category: 'anomaly' | 'recommendation' | 'trend' | 'insight';
  severity: 'low' | 'medium' | 'high' | 'critical';
  href?: string;
  ts: string; // ISO
}

export interface TopHub {
  hubId: string;
  label: string;
  description: string;
  connectionCount: number;
  tags: string[];
  signals: Signal[];
  lastUpdated: string; // ISO
}

export interface TopHubResponse {
  hub: TopHub;
  generatedAt: string;
}
```

---

### 3) Mock Data for Dev/QA (`src/utils/mock/topHubMock.ts`)

```ts
import { TopHubResponse } from '../../types/knowledge-rag';

export const topHubMock: TopHubResponse = {
  hub: {
    hubId: 'MOC',
    label: 'Month-over-month Cost',
    description: 'Central hub for cost trend analysis and anomaly detection across multi-cloud accounts.',
    connectionCount: 124,
    tags: ['cost-trend', 'anomaly', 'forecast', 'multi-cloud'],
    signals: [
      {
        id: 'sig-001',
        title: 'AWS EC2 spend spike (+38%) in us-east-1',
        summary: 'Detected unusual compute spend increase driven by un-tagged instances.',
        category: 'anomaly',
        severity: 'high',
        href: '/insights/anomalies/aws-ec2-spike-20260503',
        ts: '2026-05-03T08:12:00Z',
      },
      {
        id: 'sig-002',
        title: 'RI coverage below target (62%) for production workloads',
        summary: 'Recommend purchasing 12x m5.large RIs to reach 80% coverage.',
        category: 'recommendation',
        severity: 'medium',
        href: '/insights/recommendations/ri-coverage-20260503',
        ts: '2026-05-03T07:45:00Z',
      },
      {
        id: 'sig-003',
        title: 'Orphaned EBS volumes (12) detected',
        summary: 'Unattached volumes costing ~$180/mo; eligible for cleanup.',
        category: 'insight',
        severity: 'low',
        href: '/insights/cleanup/orphaned-volumes-20260503',
        ts: '2026-05-02T16:20:00Z',
      },
    ],
    lastUpdated: '2026-05-03T09:00:00Z',
  },
  generatedAt: '2026-05-03T09:00:00Z',
};
```

---

### 4) API Hook (`src/hooks/useTopHub.ts`)

```ts
import { useState, useEffect } from 'react';
import { TopHubResponse } from '../types/knowledge-rag';
import { topHubMock } from '../utils/mock/topHubMock';

const API_PATH = '/api/knowledge-rag/top-hub';

export function useTopHub() {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    fetch(API_PATH, { cache: 'no-store' })
      .then(async (res) => {
        if (!res.ok) throw new Error('API unavailable');
        return res.json();
      })
      .then((json) => {
        if (mounted) setData(json);
      })
      .catch(() => {
        // Fallback to mock data if API fails (read-only mode)
        if (mounted) setData(topHubMock);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  return { data, loading, error };
}
```

---

### 5) Card Component (`src/components/TopHubSignalCard/TopHubSignalCard.tsx`)

```tsx
import React from 'react';
import { TopHubResponse } from '../../types/knowledge-rag';
import { formatDistanceToNow, parseISO } from 'date-fns';

interface TopHubSignalCardProps {
  data: TopHubResponse;
}

const severityColors = {
  low: 'bg-green-500',
  medium: 'bg-yellow-500',
  high: 'bg-red-500',
  critical: 'bg-purple-600',
};

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({ data }) => {
  const { hub } = data;

  return (
    <section className="top-hub-card" aria-labelledby="hub-title">
      <header className="hub-header">
        <h2 id="hub-title" className="hub-title">Top Knowledge Hub</h2>
        <span className="hub-badge" title={`Connections: ${hub.connectionCount}`}>
          {hub.label}
        </span>
      </header>

      <p className="hub-sub">{hub.description}</p>

      <ul className="signals" aria-label="Top hub signals">
        {hub.signals.map((s) => (
          <li key={s.id} className="signal">
            <div className={`signal-dot ${severityColors[s.severity]}`} aria-label={s.severity} />
            <div className="signal-body">
              <div className="signal-title">{s.title}</div>
              <div className="signal-summary">{s.summary}</div>
              <div className="signal-meta">
                <time className="signal-ts" dateTime={s.ts}>
                  {formatDistanceToNow(parseISO(s.ts), { addSuffix: true })}
                </time>
                {s.href && (
                  <a href={s.href} className="signal-link" target="_blank" rel="noopener noreferrer">
                    View
                  </a>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>

      <footer className="hub-footer">
        <small>Sense + Signal — Read Only</small>
      </footer>
    </section>
  );
};
```

---

### 6) Page (`src/pages/Insights/KnowledgeHubPage.tsx`)

```tsx
import React from 'react';
import { TopHubSignalCard } from '../../components/TopHubSignalCard';
import { useTopHub } from '../../hooks/useTop
