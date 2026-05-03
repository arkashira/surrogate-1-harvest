# Costinel / quality

## Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub (MOC) and 3 actionable proposals from the knowledge graph. Resilient to missing backend.  
**Timebox**: <2h  
**Stack**: React + TypeScript + Tailwind  
**Quality focus**: graceful degradation, typed contracts, no runtime exceptions, copy-to-clipboard for reproducibility.

---

### 1) Core decisions (merged + resolved)
- **API path**: use `/api/graph/top-hub` (shorter, consistent with Candidate 2) and keep fallback resilient.
- **Types**: merge Candidate 1’s `KnowledgeHub/KnowledgeProposal` with Candidate 2’s `Insight` for richer signal.
- **Fetching**: use a typed hook `useTopHub()` (Candidate 2) for reusability and testability, with abort + timeout and graceful fallback.
- **UI placement**: mount in dashboard summary/sidebar area (Candidate 2) and keep compact card (Candidate 1).
- **Actionability**: include “View graph” (opens new tab) and copy-to-clipboard for the payload to aid debugging.
- **Execution boundary**: strictly read-only — no execution actions (aligns with “Sense + Signal — ไม่ Execute”).

---

### 2) File edits
- `src/types/graph.ts` — merged types.
- `src/api/graph.ts` — thin fetcher with timeout/abort.
- `src/hooks/useTopHub.ts` — typed hook with fallback.
- `src/mocks/topHubMock.json` — static MOC-centric fallback.
- `src/components/dashboard/TopHubSignalPanel.tsx` — presentational card.
- `src/pages/Dashboard.tsx` — import and mount panel.

---

### 3) Code snippets

#### `src/types/graph.ts`
```ts
export interface KnowledgeHub {
  slug: string;
  label: string;
  description?: string;
  connectionCount: number;
  lastUpdated: string; // ISO
}

export interface KnowledgeProposal {
  id: string;
  title: string;
  summary: string;
  hubSlug: string;
  actionUrl?: string;
  priority: 'high' | 'medium' | 'low';
}

export interface Insight {
  id: string;
  label: string;
  short: string;
}

export interface TopHubResponse {
  hub: KnowledgeHub;
  proposals: KnowledgeProposal[];
  insights: Insight[];
  ts: string; // ISO fetch timestamp
}
```

#### `src/mocks/topHubMock.json`
```json
{
  "hub": {
    "slug": "MOC",
    "label": "MOC",
    "description": "Most-connected operational hub",
    "connectionCount": 128,
    "lastUpdated": "2025-01-01T00:00:00.000Z"
  },
  "proposals": [
    {
      "id": "p1",
      "title": "Review cross-account egress rules",
      "summary": "High-cost egress detected between linked accounts.",
      "hubSlug": "MOC",
      "priority": "high"
    },
    {
      "id": "p2",
      "title": "Standardize RI coverage tagging",
      "summary": "Tag drift causing under-utilized reservations.",
      "hubSlug": "MOC",
      "priority": "medium"
    },
    {
      "id": "p3",
      "title": "Enable VPC Flow Logs for top 5 spoke VPCs",
      "summary": "Increase visibility into anomalous traffic patterns.",
      "hubSlug": "MOC",
      "priority": "medium"
    }
  ],
  "insights": [
    {
      "id": "i1",
      "label": "Cost signal",
      "short": "Egress spend up 22% MoM across hub-linked accounts."
    },
    {
      "id": "i2",
      "label": "Reliability signal",
      "short": "3 recurring auth failures on spoke-to-hub paths."
    }
  ],
  "ts": "2025-01-01T00:00:00.000Z"
}
```

#### `src/api/graph.ts`
```ts
import { TopHubResponse } from '../types/graph';

const API_PATH = '/api/graph/top-hub';
const TIMEOUT_MS = 8_000;

export async function fetchTopHub(signal?: AbortSignal): Promise<TopHubResponse> {
  const controller = new AbortController();
  const combinedSignal = signal
    ? (() => {
        // If outer signal aborts, abort ours too
        signal.addEventListener('abort', () => controller.abort());
        return controller.signal;
      })()
    : controller.signal;

  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const res = await fetch(API_PATH, { signal: combinedSignal });
    clearTimeout(timeout);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as TopHubResponse;
    // Basic shape validation (lightweight)
    if (!json?.hub?.slug || !Array.isArray(json.proposals)) {
      throw new Error('Invalid payload shape');
    }
    return {
      ...json,
      ts: json.ts || new Date().toISOString(),
    };
  } catch (err) {
    clearTimeout(timeout);
    throw err;
  }
}
```

#### `src/hooks/useTopHub.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import { TopHubResponse } from '../types/graph';
import { fetchTopHub } from '../api/graph';
import mock from '../mocks/topHubMock.json';

type UseTopHubReturn = {
  data: TopHubResponse | null;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
};

export function useTopHub(): UseTopHubReturn {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchTopHub();
      setData(res);
    } catch (err) {
      console.warn('Using fallback top-hub data', err);
      setData(mock as TopHubResponse);
      setError('Using fallback data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { data, loading, error, refetch: load };
}
```

#### `src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { TopHubResponse } from '../../types/graph';

interface Props {
  data: TopHubResponse;
  onOpenGraph?: () => void;
}

const priorityColor = (p: string) => {
  switch (p) {
    case 'high':
      return 'text-red-700 bg-red-50 border-red-200';
    case 'medium':
      return 'text-amber-700 bg-amber-50 border-amber-200';
    default:
      return 'text-gray-700 bg-gray-50 border-gray-200';
  }
};

export const TopHubSignalPanel: React.FC<Props> = ({ data, onOpenGraph }) => {
  const { hub, proposals, insights, ts } = data;

  const copyPayload = () => {
    try {
      navigator.clipboard.writeText(JSON.stringify(data, null, 2));
    } catch {
      // ignore
    }
  };

  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm" aria-label="Top hub signal">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{hub.label}</h3>
          <p className="text-xs
