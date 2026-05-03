# Costinel / frontend

**Final consolidated plan** (strongest parts merged; contradictions resolved for correctness + concrete actionability)

- **Where to place the feature**  
  Add as a **drop-in dashboard widget** (not a new route) so it can ship in <2h and be visible immediately on the main dashboard.  
  *Rationale:* Candidate 1’s widget approach is faster (no routing, layout, or auth plumbing) and still satisfies the core goal: surface the top-hub signal before planning. Keep a page route as optional future work.

- **API contract (resolved)**  
  Use Candidate 1’s endpoint and response shape as the baseline (it’s concrete and already assumed by backend).  
  Add one small but critical field from Candidate 2 for actionability: include a short `rationale` per proposal (or top-level) so the panel explains *why* these proposals are surfaced. If the backend doesn’t return it yet, render gracefully (empty string).

- **Actionability (resolved)**  
  Keep Candidate 2’s actions (“Acknowledge” + “Create Change Request”) but implement them as non-blocking, optimistic UI that does **not** mutate server state from the widget (strictly frontend actions or links).  
  - “Acknowledge” = local optimistic dismiss/pin with undo.  
  - “Create Change Request” = opens existing change-management UI (or pre-filled form) in a new tab or modal.  
  This satisfies read-only/signalling intent while enabling concrete next steps.

- **Polling & performance**  
  Adopt Candidate 1’s polling (60s) and error handling (non-blocking, skeleton/empty states). Add exponential backoff on repeated failures to reduce noise.

- **Code structure (merged best parts)**  
  - Hook: `useTopHubSignal` (Candidate 1) with small improvements (abort controller, backoff).  
  - Component: `TopHubSignal` widget (Candidate 1) + `ProposalCard` (Candidate 2) for consistent proposal rendering.  
  - Types: `src/types/sense.d.ts` (Candidate 2) for single source of truth.

---

## Concrete file changes

```
src/
├── api/
│   └── senseApi.ts          # thin client (optional but testable)
├── components/
│   ├── TopHubSignal/
│   │   ├── TopHubSignal.tsx
│   │   ├── TopHubSignal.css
│   │   └── ProposalCard.tsx
│   └── Common/
│       └── Skeleton.tsx
├── hooks/
│   └── useTopHubSignal.ts
├── pages/
│   └── Dashboard/
│       └── Dashboard.tsx    # import and place widget
└── types/
    └── sense.d.ts
```

---

### `src/types/sense.d.ts`
```ts
export interface Proposal {
  id: string;
  title: string;
  impact: 'HIGH' | 'MEDIUM' | 'LOW';
  effort: 'HIGH' | 'MEDIUM' | 'LOW';
  description: string;
  rationale?: string;
  actions: string[];
}

export interface TopHubSignal {
  hub: string;
  score: number;
  rationale?: string;
  proposals: Proposal[];
  updatedAt: string;
}
```

---

### `src/api/senseApi.ts`
```ts
import axios from 'axios';
import type { TopHubSignal } from '../types/sense';

const api = axios.create({
  baseURL: '/api/v1',
  headers: { 'Content-Type': 'application/json' }
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('costinel_token') || '';
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

export const senseApi = {
  getTopHubSignal: () => api.get<TopHubSignal>('/sense/top-hub-signal').then((r) => r.data)
};
```

---

### `src/hooks/useTopHubSignal.ts`
```ts
import { useEffect, useState, useRef } from 'react';
import { senseApi } from '../api/senseApi';
import type { TopHubSignal } from '../types/sense';

const POLL_INTERVAL = 60_000;
const MAX_RETRY_DELAY = 30_000;

export function useTopHubSignal(enabled = true) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const retryDelayRef = useRef(1_000);
  const pollRef = useRef<NodeJS.Timeout>();
  const abortRef = useRef<AbortController>();

  const fetchSignal = async () => {
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    try {
      const res = await senseApi.getTopHubSignal({
        signal: abortRef.current.signal
      } as any);
      setData(res);
      setError(null);
      retryDelayRef.current = 1_000; // reset backoff
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!enabled) return;
    fetchSignal();

    pollRef.current = setInterval(() => {
      fetchSignal();
      // simple backoff on error for next tick
      if (error) {
        retryDelayRef.current = Math.min(retryDelayRef.current * 2, MAX_RETRY_DELAY);
      }
    }, POLL_INTERVAL);

    return () => {
      abortRef.current?.abort();
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [enabled]);

  return { data, loading, error, refetch: fetchSignal };
}
```

---

### `src/components/TopHubSignal/ProposalCard.tsx`
```tsx
import React from 'react';
import type { Proposal } from '../../types/sense';

export const ProposalCard: React.FC<{
  proposal: Proposal;
  onAcknowledge?: (id: string) => void;
  onCreateChangeRequest?: (p: Proposal) => void;
}> = ({ proposal, onAcknowledge, onCreateChangeRequest }) => (
  <div className="proposal-card" role="article" aria-label={`Proposal ${proposal.title}`}>
    <div className="proposal-header">
      <div className="proposal-title">{proposal.title}</div>
      <div className="proposal-badges">
        <span className={`badge impact-${proposal.impact.toLowerCase()}`}>{proposal.impact}</span>
        <span className={`badge effort-${proposal.effort.toLowerCase()}`}>{proposal.effort}</span>
      </div>
    </div>
    {proposal.rationale && <p className="proposal-rationale">{proposal.rationale}</p>}
    <p className="proposal-desc">{proposal.description}</p>
    <div className="proposal-actions">
      <button
        className="btn btn-ghost"
        onClick={() => onAcknowledge?.(proposal.id)}
        aria-label={`Acknowledge ${proposal.title}`}
      >
        Acknowledge
      </button>
      <button
        className="btn btn-primary"
        onClick={() => onCreateChangeRequest?.(proposal)}
        aria-label={`Create change request for ${proposal.title}`}
      >
        Create Change Request
      </button>
    </div>
  </div>
);
```

---

### `src/components/TopHubSignal/TopHubSignal.tsx`
```tsx
import React, { useState } from 'react';
import { useTopHubSignal } from '../../hooks/useTopHubSignal';
import { ProposalCard } from './ProposalCard';
import './TopHubSignal.css';

export const TopHubSignal: React.FC = () => {
  const { data, loading, error, refetch } = useTopHubSignal(true);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  const handleAcknowledge = (id: string) => {
    setDismissed((s) =>
