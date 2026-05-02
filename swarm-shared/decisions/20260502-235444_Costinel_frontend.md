# Costinel / frontend

## Final Synthesis — Costinel Top-Hub Signal Card (Read-Only)

**Scope & Principle**  
- Read-only frontend card (≤2h).  
- “Sense + Signal — ไม่ Execute”: strictly no writes, no mutations, no background jobs, no self-execution.  

**Goal**  
Surface the most-connected hub (e.g., “MOC”) with score, summary, and actionable links by querying the knowledge-rag graph (or a CDN-cached payload) and showing top related docs/signals. Fully static/SSR-friendly, accessible, and CDN-first to avoid HF API rate limits.

---

### 1) High-value improvement (merged)
Add a persistent **Top-Hub Signal Card** to the Cost Analytics dashboard that:
- Queries the knowledge-rag graph (GET) for the top hub by degree/centrality relevant to current cost context (project/account/time), **or** uses a precomputed CDN asset (`file-list-latest.json` + `top-hub-latest.json`) for zero-latency, zero-rate-limit rendering.
- Shows hub name, score, short summary, and top 3 related docs/signals.
- Provides quick filters (date range, cloud, account) that re-query the hub without page reload (client-side GET only).
- Links to detailed hub view and related docs (opens in new tab).
- Zero writes; only GET calls to internal RAG API or CDN-backed assets.

Why this ships fast (<2h):
- Reuses existing knowledge-rag endpoints and CDN file list (no training/infra changes).
- Pure frontend component (React/TS + Tailwind) with minimal state and optional SSR/SSG support.
- Aligns to “Sense + Signal” and top-hub pattern already established.

---

### 2) Implementation steps (concrete)

1. **Create typed component**  
   `/opt/axentx/Costinel/src/components/TopHubSignalCard.tsx`

2. **Add typed interfaces and CDN/RAG helpers**  
   `/opt/axentx/Costinel/src/lib/ragApi.ts` — thin wrapper to `GET /api/knowledge-rag/top-hub`, `GET /api/knowledge-rag/hub-docs`, and CDN asset fetches (`/file-list-latest.json`, `/top-hub-latest.json`).

3. **Embed in dashboard**  
   Modify `/opt/axentx/Costinel/src/pages/CostDashboard.tsx` to include `<TopHubSignalCard />` in sidebar or top-row.

4. **Add CDN assets (optional but recommended)**  
   Generate and place JSON assets at `/public/top-hub-latest.json` and `/public/file-list-latest.json` via orchestration script so training/embedding scripts can use CDN-only fetches.

5. **Style with Tailwind** to match existing card patterns and ensure accessibility (semantic HTML, aria labels, focus states).

6. **Add lightweight tests**  
   - Unit snapshot test for the component.  
   - One e2e smoke selector (e.g., `data-testid="top-hub-signal-card"`).

7. **Validate read-only behavior**  
   - Confirm no POST/PUT/DELETE in network tab during interactions.  
   - Ensure filters trigger only GET requests.

---

### 3) Code snippets (merged + corrected)

#### `/opt/axentx/Costinel/src/lib/ragApi.ts`
```ts
// Lightweight RAG API + CDN helpers (read-only)
const API_BASE = '/api/knowledge-rag';

export interface HubSummary {
  hub: string;
  score: number;
  summary: string;
  [k: string]: unknown;
}

export interface DocLink {
  title: string;
  snippet?: string;
  url: string;
  [k: string]: unknown;
}

export interface HubDocs {
  docs: DocLink[];
}

export interface CdnTopHub {
  date: string;
  hub: string;
  score: number;
  summary: string;
  files: string[];
  cdnBase?: string;
}

// Fetch top hub from RAG API (context-aware)
export async function fetchTopHub(filters: {
  projectId?: string;
  accountId?: string;
  startDate?: string;
  endDate?: string;
} = {}): Promise<HubSummary> {
  const params = new URLSearchParams();
  if (filters.projectId) params.append('projectId', filters.projectId);
  if (filters.accountId) params.append('accountId', filters.accountId);
  if (filters.startDate) params.append('startDate', filters.startDate);
  if (filters.endDate) params.append('endDate', filters.endDate);

  const res = await fetch(`${API_BASE}/top-hub?${params.toString()}`, {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  });

  if (!res.ok) throw new Error('Failed to fetch top hub');
  return res.json() as Promise<HubSummary>;
}

// Fetch related docs for a hub
export async function fetchHubDocs(hub: string, limit = 3): Promise<HubDocs> {
  const res = await fetch(`${API_BASE}/hub-docs?hub=${encodeURIComponent(hub)}&limit=${limit}`, {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  });

  if (!res.ok) throw new Error('Failed to fetch hub docs');
  return res.json() as Promise<HubDocs>;
}

// CDN-only fetch for precomputed top hub (zero rate-limit)
export async function fetchCdnTopHub(cdnPath = '/top-hub-latest.json'): Promise<CdnTopHub | null> {
  try {
    const res = await fetch(cdnPath, {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-cache',
    });
    if (!res.ok) return null;
    return res.json() as Promise<CdnTopHub>;
  } catch {
    return null;
  }
}
```

#### `/opt/axentx/Costinel/src/components/TopHubSignalCard.tsx`
```tsx
import { useEffect, useState, useCallback } from 'react';
import { fetchTopHub, fetchHubDocs, fetchCdnTopHub, type HubSummary, type DocLink, type CdnTopHub } from '../lib/ragApi';

export interface TopHubSignalCardProps {
  filters?: {
    projectId?: string;
    accountId?: string;
    startDate?: string;
    endDate?: string;
  };
  preferCdn?: boolean;
}

export default function TopHubSignalCard({ filters = {}, preferCdn = true }: TopHubSignalCardProps) {
  const [hub, setHub] = useState<HubSummary | null>(null);
  const [docs, setDocs] = useState<DocLink[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      let hubResult: HubSummary | null = null;

      if (preferCdn) {
        const cdn = await fetchCdnTopHub();
        if (cdn) {
          hubResult = { hub: cdn.hub, score: cdn.score, summary: cdn.summary };
        }
      }

      if (!hubResult) {
        hubResult = await fetchTopHub(filters);
      }

      setHub(hubResult);
      const related = await fetchHubDocs(hubResult.hub, 3);
      setDocs(related.docs || []);
    } catch (err: any) {
      setError(err?.message || 'Failed to load hub signal');
    } finally {
      setLoading(false);
    }
  }, [filters, preferCdn]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters), preferCdn]);

  if (loading) {
    return (
      <div className="p-4 border rounded-lg bg-white shadow-sm" data-testid="top-hub-signal-card">
        <p className="text-sm text-gray-500">Loading top hub signal...</p>
      </div>
    );
  }

  if (error) {
   
