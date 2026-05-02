# Costinel / frontend

## Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, context excerpt, and related docs. Data is fetched read-only from a JSON endpoint (or static file) produced by the knowledge-rag pipeline.

### What I’ll ship (highest-value incremental improvement)
- A compact, production-ready React card component (`TopHubSignalCard`) that:
  - Shows hub name, score, and short rationale.
  - Lists top 3 related docs with links.
  - Auto-refreshes every 5m (configurable) via polling.
  - Graceful fallback when data is missing.
- A minimal JSON contract (`top-hub.json`) the backend/knowledge-rag pipeline can write.
- One-line integration into the existing dashboard layout.
- No state mutations, no writes, no side effects — strictly read-only.

### Implementation steps (≤2h)
1. Add JSON contract and sample data (`public/data/top-hub.json`).  
2. Create `src/components/TopHubSignalCard/TopHubSignalCard.tsx` + styles.  
3. Add a small hook (`useTopHubPoll`) for polling and error handling.  
4. Wire into the main dashboard (likely `src/pages/Dashboard/Dashboard.tsx` or equivalent) in the “Insights” or “Signals” section.  
5. Verify visual fit and responsiveness.

---

### 1) JSON contract (public/data/top-hub.json)

```json
{
  "hub": "MOC",
  "score": 94.2,
  "rationale": "Highest betweenness centrality across cost-governance topics; strong linkage to RI, anomaly, and policy signals.",
  "relatedDocs": [
    { "title": "Reserved Instance Playbook", "url": "/docs/ri-playbook", "type": "doc" },
    { "title": "Anomaly Detection Guide", "url": "/docs/anomalies", "type": "doc" },
    { "title": "Policy-as-Code Examples", "url": "/docs/policy-examples", "type": "doc" }
  ],
  "updatedAt": "2026-05-03T08:12:00Z"
}
```

---

### 2) Component: TopHubSignalCard.tsx

```tsx
// src/components/TopHubSignalCard/TopHubSignalCard.tsx
import React from 'react';
import { useTopHubPoll } from '../../hooks/useTopHubPoll';
import './TopHubSignalCard.css';

export interface RelatedDoc {
  title: string;
  url: string;
  type: string;
}

export interface TopHubPayload {
  hub: string;
  score: number;
  rationale: string;
  relatedDocs: RelatedDoc[];
  updatedAt: string;
}

const DEFAULT_DATA: TopHubPayload = {
  hub: '—',
  score: 0,
  rationale: 'No signal available',
  relatedDocs: [],
  updatedAt: new Date().toISOString(),
};

export const TopHubSignalCard: React.FC<{
  pollIntervalMs?: number;
  endpoint?: string;
}> = ({ pollIntervalMs = 5 * 60 * 1000, endpoint = '/data/top-hub.json' }) => {
  const { data, loading, error } = useTopHubPoll(endpoint, pollIntervalMs);
  const hub = loading && !data ? null : data || DEFAULT_DATA;

  return (
    <div className="top-hub-card" role="region" aria-label="Top hub signal">
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <span className="top-hub-name">{hub.hub}</span>
        <span className="top-hub-score" title="Signal score">
          {typeof hub.score === 'number' ? hub.score.toFixed(1) : '—'}
        </span>
      </div>

      <p className="top-hub-rationale">{hub.rationale}</p>

      {hub.relatedDocs.length > 0 && (
        <ul className="top-hub-docs" aria-label="Related documents">
          {hub.relatedDocs.map((doc, idx) => (
            <li key={idx}>
              <a href={doc.url} target="_blank" rel="noopener noreferrer">
                {doc.title}
              </a>
            </li>
          ))}
        </ul>
      )}

      <div className="top-hub-footer">
        <small>
          Updated: {hub.updatedAt ? new Date(hub.updatedAt).toLocaleString() : '—'}
        </small>
        {error && <small className="error-note"> (stale data)</small>}
      </div>
    </div>
  );
};
```

---

### 3) Hook: useTopHubPoll.ts

```ts
// src/hooks/useTopHubPoll.ts
import { useEffect, useState, useRef } from 'react';
import { TopHubPayload } from '../components/TopHubSignalCard/TopHubSignalCard';

export function useTopHubPoll(url: string, intervalMs: number) {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<boolean>(false);
  const timerRef = useRef<number | null>(null);

  const fetchData = async () => {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as TopHubPayload;
      setData(json);
      setError(false);
    } catch (err) {
      console.warn('Top-hub poll failed:', err);
      setError(true);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    timerRef.current = window.setInterval(fetchData, intervalMs);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [url, intervalMs]);

  return { data, loading, error };
}
```

---

### 4) Styles: TopHubSignalCard.css

```css
/* src/components/TopHubSignalCard/TopHubSignalCard.css */
.top-hub-card {
  border: 1px solid #e6e9ef;
  border-radius: 10px;
  padding: 16px;
  background: #fff;
  max-width: 360px;
  box-shadow: 0 1px 3px rgba(16,24,40,0.06);
}

.top-hub-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.top-hub-badge {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #6b7280;
}

.top-hub-name {
  font-size: 20px;
  font-weight: 700;
  color: #0f172a;
}

.top-hub-score {
  font-size: 14px;
  font-weight: 600;
  color: #059669;
  margin-left: auto;
}

.top-hub-rationale {
  margin: 0 0 10px 0;
  font-size: 13px;
  color: #475569;
  line-height: 1.4;
}

.top-hub-docs {
  list-style: none;
  padding: 0;
  margin: 0 0 10px 0;
  font-size: 13px;
}

.top-hub-docs li {
  margin-bottom: 4px;
}

.top-hub-d
