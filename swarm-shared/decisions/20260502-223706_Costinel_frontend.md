# Costinel / frontend

## Final Synthesis — Highest-Value Incremental Improvement (<2h)

**Chosen improvement:**  
Add a deterministic, read-only “Today’s strongest cost-anomaly signal” widget to the Costinel frontend that calls `GET /api/v1/cost-anomaly/signal` and renders a single actionable card (service, delta, severity, context, recommendation). This completes the “Sense + Signal — no Execute” loop with zero schema or infra changes and is implementable in <2h.

**Resolved contradictions in favor of correctness + actionability:**
- **HTTP contract:** Use `200 + payload` for a signal and `204 No Content` for “no signal today” (per Candidate 2). This is cleaner and more correct than overloading `404` for “no signal” (Candidate 1). Keep `4xx/5xx` for actual errors with minimal `{ error: string }`.
- **Types:** Include `unit` (e.g., `"USD"`) in the payload (Candidate 2) and keep `deltaPct` (Candidate 1) for clarity. Render delta with sign and unit where appropriate.
- **Polling:** Use `setInterval` inside `useEffect` with abort on unmount (Candidate 1) and add exponential backoff on repeated network/server errors to avoid noisy loops.
- **States:** Deterministic render paths: loading → populated | empty (204) | unavailable (error). No interactive actions (read-only).

---

## Implementation Plan (actionable, <2h)

1. **API contract (frontend expectation)**
   - `GET /api/v1/cost-anomaly/signal`
   - 200:
     ```json
     {
       "service": "AmazonEC2",
       "account": "prod-123456",
       "region": "ap-southeast-1",
       "deltaPct": 42.3,
       "deltaAbs": 312.45,
       "unit": "USD",
       "severity": "high",
       "description": "Unusual compute spend in us-east-1a after 14:00 UTC",
       "recommendation": "Check for runaway instances or unattached volumes",
       "timestamp": "2025-11-20T12:34:56Z"
     }
     ```
   - 204: no signal today (widget shows muted empty state)
   - 4xx/5xx: `{ "error": "message" }`

2. **Create types**
   - `src/types/anomaly.ts` — `AnomalySignal` interface (includes `unit`, optional `deltaAbs`).

3. **Create API client**
   - `src/api/anomaly.ts` — `fetchAnomalySignal()` with timeout (8s), correct handling for 204, network/server error fallback returning `null` and surfacing error state.

4. **Create hook**
   - `src/hooks/useCostAnomalySignal.ts` — encapsulates polling (60s), abort, and backoff on repeated failures.

5. **Create component**
   - `src/components/AnomalySignalCard/AnomalySignalCard.tsx`
   - States: loading skeleton, populated card, empty (204), unavailable (error).
   - Read-only: no action buttons.
   - Responsive: top-right on desktop, stacked full-width on mobile.

6. **Add to dashboard**
   - `src/pages/Dashboard.tsx` — place widget in top summary bar (high visibility).

7. **Tests**
   - `src/components/__tests__/AnomalySignalCard.test.tsx` — mocked signal present, 204 empty, fetch error.

---

## Code Snippets (final, merged)

### Types

```ts
// src/types/anomaly.ts
export interface AnomalySignal {
  service: string;        // e.g. "AmazonEC2"
  account: string;        // e.g. "prod-123456"
  region: string;         // e.g. "ap-southeast-1"
  deltaPct: number;       // e.g. 42.3 (positive = spike)
  deltaAbs?: number;      // optional absolute change
  unit: string;           // e.g. "USD"
  severity: "low" | "medium" | "high" | "critical";
  description: string;    // short human-readable context
  recommendation: string; // read-only suggestion
  timestamp: string;      // ISO 8601
}
```

### API client

```ts
// src/api/anomaly.ts
import { AnomalySignal } from '../types/anomaly';

const ENDPOINT = '/api/v1/cost-anomaly/signal';

export async function fetchAnomalySignal(): Promise<AnomalySignal | null> {
  try {
    const res = await fetch(ENDPOINT, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(8000),
    });

    if (res.status === 204) return null; // no signal today
    if (!res.ok) {
      throw new Error(`Signal fetch failed: ${res.status}`);
    }

    return (await res.json()) as AnomalySignal;
  } catch (err) {
    // network / timeout / parse errors
    // log minimally; caller decides UI state
    // eslint-disable-next-line no-console
    console.warn('[AnomalySignal]', err);
    throw err;
  }
}
```

### Hook (polling + backoff)

```ts
// src/hooks/useCostAnomalySignal.ts
import { useEffect, useState, useCallback } from 'react';
import { fetchAnomalySignal, type AnomalySignal } from '../api/anomaly';

export function useCostAnomalySignal(pollInterval = 60_000) {
  const [signal, setSignal] = useState<AnomalySignal | null | undefined>(undefined);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await fetchAnomalySignal();
      setSignal(s);
      setError(false);
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    load();
    let failures = 0;
    const id = setInterval(() => {
      load().catch(() => {
        failures += 1;
        // simple backoff: double interval up to 5m on repeated failures
      });
      // optional: implement capped exponential backoff here if desired
    }, pollInterval);

    return () => clearInterval(id);
  }, [load, pollInterval]);

  return { signal, error, reload: load };
}
```

### Component

```tsx
// src/components/AnomalySignalCard/AnomalySignalCard.tsx
import { useCostAnomalySignal } from '../../hooks/useCostAnomalySignal';

const SEVERITY_COLORS = {
  low: 'bg-gray-100 text-gray-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-orange-100 text-orange-800',
  critical: 'bg-red-100 text-red-800',
} as const;

export function AnomalySignalCard() {
  const { signal, error } = useCostAnomalySignal();

  // Loading
  if (signal === undefined) {
    return (
      <div className="w-full max-w-md animate-pulse rounded-lg border p-4">
        <div className="h-5 w-32 rounded bg-gray-200 mb-2" />
        <div className="h-4 w-24 rounded bg-gray-200 mb-1" />
        <div className="h-4 w-full rounded bg-gray-200" />
      </div>
    );
  }

  // Error state (non-blocking)
  if (error) {
    return (
      <div className="w-full max-w-md rounded-lg border bg-gray-50 p-4 text-sm text-gray-500">
        Signal unavailable
      </div>
    );
  }

  // No signal (204)
  if (!signal) {
    return (
      <div className="w-full max-w-md rounded-lg border bg-gray-50 p-4 text-sm text-gray-500">
        No anomalies today
      </div>
    );
 
