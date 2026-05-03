# Costinel / quality

## Final Implementation Plan — Top-hub Signal Panel (Costinel dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Assumptions**: React + Tailwind stack; resilient to missing API (graceful fallback UI).

---

### 1) Types (`src/types/knowledge.ts`)

```ts
export interface KnowledgeHub {
  id: string;
  label: string;
  type: 'hub';
  connections: number;
  lastUpdated: string;
  metadata?: Record<string, unknown>;
}

export interface KnowledgeProposal {
  id: string;
  title: string;
  summary: string;
  hubId: string;
  impact: 'high' | 'medium' | 'low';
  tags: string[];
  createdAt: string;
  href?: string;
}

export interface TopHubPayload {
  hub: KnowledgeHub | null;
  proposals: KnowledgeProposal[];
}
```

---

### 2) API Fetcher (`src/lib/api/knowledge.ts`)

```ts
import { TopHubPayload } from '@/types/knowledge';

const API_PATH = '/api/graph/hubs/top';

export async function fetchTopHub(): Promise<TopHubPayload> {
  const res = await fetch(API_PATH, { credentials: 'include' });
  if (!res.ok) {
    const err = new Error(`Failed to fetch top hub: ${res.status}`);
    // Attach status for optional handling by callers
    (err as any).status = res.status;
    throw err;
  }
  return res.json();
}
```

---

### 3) Component (`src/components/dashboard/TopHubSignalPanel.tsx`)

```tsx
import { useEffect, useState, useCallback } from 'react';
import { fetchTopHub } from '@/lib/api/knowledge';
import type { TopHubPayload } from '@/types/knowledge';

const impactColors = {
  high: 'bg-rose-50 border-rose-200 text-rose-800',
  medium: 'bg-amber-50 border-amber-200 text-amber-800',
  low: 'bg-emerald-50 border-emerald-200 text-emerald-800',
} as const;

export default function TopHubSignalPanel() {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    let mounted = true;
    setLoading(true);
    try {
      const data = await fetchTopHub();
      if (!mounted) return;
      setPayload(data);
      setError(null);
    } catch (err: any) {
      if (!mounted) return;
      setError(err?.message || 'Failed to load top-hub signals');
      setPayload(null);
    } finally {
      if (mounted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    return () => {
      // cleanup handled in load()
    };
  }, [load]);

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="h-5 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-3 h-4 w-24 animate-pulse rounded bg-gray-100" />
        <div className="mt-4 space-y-2">
          <div className="h-10 w-full animate-pulse rounded bg-gray-50" />
          <div className="h-10 w-full animate-pulse rounded bg-gray-50" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-yellow-100 bg-yellow-50 p-4 shadow-sm">
        <div className="flex items-center justify-between">
          <p className="text-sm text-yellow-800">Unable to load top-hub signals.</p>
          <button
            onClick={load}
            className="text-sm font-medium text-yellow-700 hover:underline"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const hubLabel = payload?.hub?.label || payload?.hub?.id || '—';
  const proposals = payload?.proposals ?? [];

  if (!proposals.length) {
    return (
      <div className="rounded-lg border border-gray-100 bg-gray-50 p-4 shadow-sm">
        <p className="text-sm text-gray-600">No active signals for the top hub at this time.</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-baseline justify-between">
        <h3 className="text-base font-semibold text-gray-900">Top-hub Signal</h3>
        <span className="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
          {hubLabel}
        </span>
      </div>
      <p className="mt-1 text-sm text-gray-500">
        {payload?.hub?.connections != null ? `${payload.hub.connections} connections` : ''}
      </p>

      <ul className="mt-4 space-y-3" role="list">
        {proposals.map((p) => (
          <li key={p.id} className="relative">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-gray-900">{p.title}</p>
                <p className="mt-0.5 text-sm text-gray-600">{p.summary}</p>
              </div>
              {p.href && (
                <a
                  href={p.href}
                  className="whitespace-nowrap text-sm font-medium text-blue-600 hover:underline"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View
                </a>
              )}
            </div>
            {p.impact && (
              <span
                className={`mt-2 inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${
                  impactColors[p.impact] || 'bg-gray-50 text-gray-600'
                }`}
              >
                {p.impact}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

---

### 4) Dashboard Integration (`src/components/dashboard/Dashboard.tsx`)

```tsx
import TopHubSignalPanel from '@/components/dashboard/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Cost Governance Dashboard</h1>
        <p className="text-sm text-gray-600">Sense + Signal — ไม่ Execute</p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-1">
          <TopHubSignalPanel />
          {/* other cards... */}
        </div>

        <div className="lg:col-span-2">
          {/* existing cost charts/tables */}
        </div>
      </div>
    </div>
  );
}
```

---

### 5) API Contract (Frontend expectation)

Endpoint: `GET /api/graph/hubs/top`  
Response (JSON):

```json
{
  "hub": {
    "id": "moc",
    "label": "MOC",
    "type": "hub",
    "connections": 42,
