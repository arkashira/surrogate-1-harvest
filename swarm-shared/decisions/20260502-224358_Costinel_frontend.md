# Costinel / frontend

## Implementation Plan — Costinel Frontend Quality Increment (<2h)

**Highest-value improvement:**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint (backend) + frontend hook/component that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal. This directly applies the **top-hub doc insight** pattern and gives immediate operational value without write-side risk.

### Scope (frontend focus)
- Add a React hook `useTopHubCostSignal()` that fetches `/api/v1/cost-anomaly/signal/top-hub`
- Add a small dashboard widget `TopHubAnomalyCard` that renders signal + context
- Ensure zero runtime mutations (read-only) and graceful fallback when graph data is unavailable
- Keep bundle impact minimal (<5kB)

Estimated effort: ~90 minutes (60m code, 30m integration/test).

---

## Implementation

### 1) API contract (frontend expects)

```json
GET /api/v1/cost-anomaly/signal/top-hub
→ 200
{
  "hub": "MOC",
  "signal": "spend_spike",
  "severity": "high",
  "score": 0.92,
  "window": "2026-05-02T00:00:00Z/2026-05-03T00:00:00Z",
  "description": "MOC shows 2.4× spend spike vs 7d baseline; primary driver: us-east-1 EC2.",
  "recommendation": "Review idle instances and RI coverage for us-east-1.",
  "contextLinks": [
    { "label": "MOC hub details", "href": "/hubs/MOC" },
    { "label": "Affected accounts", "href": "/accounts?hub=MOC" }
  ]
}
```

404/empty → frontend renders “No signal today”.

---

### 2) Frontend hook (`src/hooks/useTopHubCostSignal.ts`)

```ts
// src/hooks/useTopHubCostSignal.ts
import { useQuery, UseQueryOptions } from '@tanstack/react-query';

export interface TopHubSignal {
  hub: string;
  signal: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  score: number;
  window: string;
  description: string;
  recommendation: string;
  contextLinks: Array<{ label: string; href: string }>;
}

async function fetchTopHubSignal(): Promise<TopHubSignal | null> {
  const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  });

  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Failed to fetch top-hub signal: ${res.status}`);
  return res.json();
}

export function useTopHubCostSignal(
  options?: Omit<UseQueryOptions<TopHubSignal | null>, 'queryKey' | 'queryFn'>
) {
  return useQuery<TopHubSignal | null>({
    queryKey: ['cost-anomaly', 'top-hub', 'signal'],
    queryFn: fetchTopHubSignal,
    staleTime: 5 * 60 * 1000, // 5m
    refetchInterval: 10 * 60 * 1000, // 10m
    ...options,
  });
}
```

---

### 3) Widget component (`src/components/TopHubAnomalyCard.tsx`)

```tsx
// src/components/TopHubAnomalyCard.tsx
import React from 'react';
import { useTopHubCostSignal } from '../hooks/useTopHubCostSignal';
import { ExternalLink } from 'lucide-react';

const severityColors = {
  low: 'bg-blue-100 text-blue-800 border-blue-200',
  medium: 'bg-yellow-100 text-yellow-800 border-yellow-200',
  high: 'bg-orange-100 text-orange-800 border-orange-200',
  critical: 'bg-red-100 text-red-800 border-red-200',
} as const;

export function TopHubAnomalyCard() {
  const { data, isLoading, isError } = useTopHubCostSignal();

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-2 h-4 w-24 animate-pulse rounded bg-gray-100" />
      </div>
    );
  }

  if (isError || !data) {
    return null; // silent fallback (no signal today)
  }

  const colorClass = severityColors[data.severity] ?? severityColors.medium;

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-semibold text-gray-900">{data.hub}</span>
            <span
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${colorClass}`}
            >
              {data.signal}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-600">{data.description}</p>
          <p className="mt-2 text-xs text-gray-500">Window: {data.window}</p>
        </div>
      </div>

      <div className="mt-3">
        <p className="text-sm text-gray-700">
          <span className="font-medium">Recommendation:</span> {data.recommendation}
        </p>
      </div>

      {data.contextLinks.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {data.contextLinks.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
            >
              {link.label}
              <ExternalLink className="h-3 w-3" />
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
```

---

### 4) Add widget to dashboard (`src/pages/Dashboard.tsx` snippet)

Insert near top of dashboard summary row:

```tsx
import { TopHubAnomalyCard } from '../components/TopHubAnomalyCard';

// inside Dashboard return:
<div className="grid gap-6 lg:grid-cols-3">
  <div className="lg:col-span-2">
    {/* existing cost overview */}
  </div>
  <div className="lg:col-span-1">
    <TopHubAnomalyCard />
  </div>
</div>
```

---

### 5) Tests (minimal, high value)

Add one unit test for hook behavior (mock fetch):

```ts
// src/hooks/__tests__/useTopHubCostSignal.test.ts
import { renderHook } from '@testing-library/react';
import { useTopHubCostSignal } from '../useTopHubCostSignal';

global.fetch = jest.fn();

describe('useTopHubCostSignal', () => {
  beforeEach(() => {
    (global.fetch as jest.Mock).mockClear();
  });

  it('returns parsed signal on success', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        hub: 'MOC',
        signal: 'spend_spike',
        severity: 'high',
        score: 0.92,
        window: '2026-05-02T00:00:00Z/2026-05-03
