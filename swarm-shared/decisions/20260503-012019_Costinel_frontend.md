# Costinel / frontend

## Final synthesized implementation (highest-value, correct, actionable)

**Chosen improvement:** Add a **Top-hub signal panel** to the Costinel dashboard that surfaces the most-connected hub (e.g., "MOC") and actionable proposals from the knowledge graph.  
**Why:** Highest user impact per effort—visible on the dashboard, reuses the planned backend `GET /api/v1/sense/top-hub-signal`, no schema/build changes, and fits a <2h scope.

---

## Resolved design choices (favoring correctness + actionability)

- **Location for types**: `src/types/sense.ts` (co-locate with domain; avoids one-off files).
- **API helper**: `src/api/sense.ts` (consistent with existing patterns; single responsibility).
- **Component location**: `src/components/dashboard/TopHubSignalPanel.tsx` (colocated with dashboard concerns; clearer ownership than generic `components/`).
- **Data fetching**: lightweight hook (`useTopHubSignal`) inside the component file for now (fast to ship); promote to `src/hooks/` later if reused.
- **Polling**: 60s interval while mounted; exponential backoff on failure; manual refresh button.
- **UX states**: loading (initial), empty, error, data. Copy-to-clipboard for proposals.
- **Styling**: reuse existing Card/Button/Icon tokens; match Costinel design tokens.
- **Confidence/rationale**: include in proposal display (from Candidate 2) to support governance decisions.

---

## Implementation plan (≤2h)

1. Add TypeScript interface (`src/types/sense.ts`).
2. Add API helper (`src/api/sense.ts`) with bearer token auth.
3. Create `TopHubSignalPanel` component with:
   - `useTopHubSignal` hook (polling + exponential backoff + manual refresh).
   - Loading / empty / error / data states.
   - Proposal list with title, description, confidence, rationale, actions.
   - Copy-to-clipboard for each proposal.
4. Wire panel into dashboard layout (`src/pages/Dashboard.tsx`) in a prominent card slot.
5. Smoke-test against backend endpoint and polish.

---

## Code

### 1) Type definition (`src/types/sense.ts`)

```ts
export interface TopHubSignal {
  hub: string;
  insight: string;
  generatedAt: string; // ISO
  proposals: Array<{
    id: string;
    title: string;
    description: string;
    confidence?: number;      // 0..1 optional
    rationale?: string;
    actions: string[];
    context?: Record<string, unknown>;
  }>;
}
```

### 2) API helper (`src/api/sense.ts`)

```ts
import axios from '../lib/axios';
import { TopHubSignal } from '../types/sense';

export async function fetchTopHubSignal(): Promise<TopHubSignal> {
  const { data } = await axios.get<TopHubSignal>('/api/v1/sense/top-hub-signal');
  return data;
}
```

### 3) Component (`src/components/dashboard/TopHubSignalPanel.tsx`)

```tsx
import React, { useEffect, useState, useCallback } from 'react';
import { fetchTopHubSignal } from '../../api/sense';
import { TopHubSignal } from '../../types/sense';
import { Card, CardHeader, CardContent } from '../ui/Card';
import { Button } from '../ui/Button';
import { CopyIcon, RefreshIcon, AlertCircle } from '../ui/Icons';

const POLL_INTERVAL_MS = 60_000;
const MAX_BACKOFF_MS = 30_000;

export const TopHubSignalPanel: React.FC = () => {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [backoff, setBackoff] = useState(0);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await fetchTopHubSignal();
      setSignal(data);
      setBackoff(0);
    } catch (err: any) {
      setError(err.message || 'Failed to load top-hub signal');
    } finally {
      setLoading(false);
    }
  }, []);

  // polling + exponential backoff on failure
  useEffect(() => {
    load();
    const id = setInterval(() => {
      load();
      // increase backoff after failures up to cap
      setBackoff((b) => Math.min(b + POLL_INTERVAL_MS, MAX_BACKOFF_MS));
    }, POLL_INTERVAL_MS + backoff);
    return () => clearInterval(id);
  }, [load, backoff]);

  const copyProposal = (proposal: any) => {
    navigator.clipboard.writeText(JSON.stringify(proposal, null, 2)).catch(() => {
      // ignore; best-effort
    });
  };

  const refresh = () => {
    setBackoff(0);
    load();
  };

  if (loading && !signal) {
    return (
      <Card>
        <CardContent className="p-6 text-center text-sm text-gray-500">
          Loading top-hub signal…
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card variant="error">
        <CardContent className="p-4">
          <div className="flex items-start gap-2 text-sm text-red-700">
            <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <div className="flex-1">{error}</div>
          </div>
          <div className="mt-3">
            <Button size="sm" onClick={refresh}>
              <RefreshIcon className="w-4 h-4 mr-1" />
              Retry
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!signal || !signal.proposals.length) {
    return (
      <Card>
        <CardContent className="p-6 text-center text-sm text-gray-500">
          No active signals at the moment.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title={`Top Hub: ${signal.hub}`}
        subtitle={new Date(signal.generatedAt).toLocaleString()}
        action={
          <Button size="sm" variant="ghost" onClick={refresh} aria-label="Refresh signal">
            <RefreshIcon className="w-4 h-4" />
          </Button>
        }
      />
      <CardContent>
        <p className="mb-4 text-sm text-gray-600">{signal.insight}</p>
        <ul className="space-y-3">
          {signal.proposals.map((p) => (
            <li
              key={p.id}
              className="border border-gray-100 rounded p-3 bg-gray-50/50 flex flex-col gap-2"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <h4 className="font-medium text-sm text-gray-900 truncate">{p.title}</h4>
                  <p className="text-xs text-gray-600 mt-1">{p.description}</p>
                  {(p.confidence != null || p.rationale) && (
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
                      {p.confidence != null && (
                        <span title="Confidence">
                          Confidence: {Math.round(p.confidence * 100)}%
                        </span>
                      )}
                      {p.rationale && (
                        <span className="line-clamp-1">{p.rationale}</span>
                      )}
                    </div>
                  )}
                  {p.actions.length > 0 && (
                    <ul className="mt-2 text-xs text-gray-600 list-disc list-inside">
                      {p.actions.map((a, ai) => (
                        <li key={ai}>{a}</li>
                      ))}
                    </ul>
                  )}
                </div>

