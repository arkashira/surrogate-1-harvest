# Costinel / frontend

## Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, context, and audit trail in a single card on the dashboard.

---

### 1) Highest-value incremental improvement
Add a **Top-Hub Signal Card** to the dashboard that:
- Shows the highest-scored hub (from knowledge-rag graph) with score, short context, and timestamp.
- Links to the hub detail page (read-only).
- Displays an audit trail (who/when last updated) — strictly read-only.
- Uses CDN-bypass pattern for any dataset assets (if needed) and avoids any write/execute paths.

Estimated effort: ~90 minutes (React component + hook + styles).

---

### 2) Implementation steps

1. Add a new API route (read-only) to fetch top hub from knowledge-rag (or local cache)  
   - Path: `/api/top-hub` (GET)  
   - Response: `{ hubId, label, score, context, updatedAt, updatedBy }`

2. Create a `useTopHub` hook to fetch and refresh (poll every 5m)  
   - Uses `fetch` with CDN-friendly headers; no auth for public dataset paths if applicable.

3. Add `TopHubSignalCard` component to the dashboard layout  
   - Compact card with score badge, context excerpt, and link to hub detail.
   - Shows last-updated audit line.

4. Wire into existing dashboard grid (replace placeholder or add to top row).

5. Ensure zero side effects: no POST/PUT/DELETE, no state mutations on backend, no cron/wrapper execution from frontend.

---

### 3) Code snippets

#### `src/api/topHub.ts` (client-side hook)
```ts
// src/api/topHub.ts
export interface TopHub {
  hubId: string;
  label: string;
  score: number;
  context: string;
  updatedAt: string;
  updatedBy: string;
}

export async function fetchTopHub(): Promise<TopHub> {
  const res = await fetch('/api/top-hub', {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
    },
    cache: 'no-store',
  });

  if (!res.ok) {
    throw new Error('Failed to fetch top hub');
  }
  return res.json();
}

export function useTopHub(pollIntervalMs = 300_000) {
  const [hub, setHub] = React.useState<TopHub | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<Error | null>(null);

  const load = React.useCallback(async () => {
    try {
      setLoading(true);
      const data = await fetchTopHub();
      setHub(data);
      setError(null);
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    load();
    const id = setInterval(load, pollIntervalMs);
    return () => clearInterval(id);
  }, [load, pollIntervalMs]);

  return { hub, loading, error, refetch: load };
}
```

#### `src/components/TopHubSignalCard.tsx`
```tsx
// src/components/TopHubSignalCard.tsx
import React from 'react';
import { useTopHub } from '../api/topHub';
import { Link } from 'react-router-dom';

export function TopHubSignalCard() {
  const { hub, loading, error } = useTopHub();

  if (loading) {
    return (
      <div className="p-4 rounded-lg border border-gray-200 bg-white">
        <p className="text-sm text-gray-500">Loading top hub signal…</p>
      </div>
    );
  }

  if (error || !hub) {
    return (
      <div className="p-4 rounded-lg border border-gray-200 bg-white">
        <p className="text-sm text-red-600">Unable to load top hub signal.</p>
      </div>
    );
  }

  return (
    <div className="p-4 rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-semibold text-gray-900">Top Hub Signal</h3>
            <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700">
              {hub.score.toFixed(2)}
            </span>
          </div>
          <p className="mt-1 text-sm font-medium text-gray-800">{hub.label}</p>
          <p className="mt-2 text-sm text-gray-600 line-clamp-2">{hub.context}</p>
          <div className="mt-3 text-xs text-gray-500">
            Updated {new Date(hub.updatedAt).toLocaleString()} by {hub.updatedBy}
          </div>
        </div>
        <Link
          to={`/hubs/${encodeURIComponent(hub.hubId)}`}
          className="text-sm font-medium text-blue-600 hover:underline"
        >
          View details →
        </Link>
      </div>
    </div>
  );
}
```

#### Backend stub (Node/Express example — read-only)
```ts
// src/server/routes/topHubRoute.ts
import express from 'express';
import { getTopHubFromGraph } from '../../services/knowledgeRagService';

const router = express.Router();

router.get('/api/top-hub', async (req, res) => {
  try {
    // Read-only: query graph/cache; no writes, no execution.
    const hub = await getTopHubFromGraph(); // returns { hubId, label, score, context, updatedAt, updatedBy }
    res.json(hub);
  } catch (err) {
    console.error('Top hub fetch failed', err);
    res.status(500).json({ error: 'Failed to fetch top hub' });
  }
});

export default router;
```

#### Add to dashboard grid (example)
```tsx
// In your dashboard layout
import { TopHubSignalCard } from '../components/TopHubSignalCard';

// Place in grid
<TopHubSignalCard />
```

---

### 4) Acceptance criteria (read-only)
- Card renders on dashboard with top hub label, score, and context.
- Score and context update when backend data changes (poll every 5m).
- Link to hub detail opens read-only hub page.
- No POST/PUT/DELETE requests originate from this card.
- No cron/wrapper/exec side effects triggered.
- Audit line shows updatedAt and updatedBy.

---

### 5) Notes & patterns applied
- **Sense + Signal only**: strictly read-only; no execution.  
- **Top-hub doc insight**: prioritize most-connected hub (e.g., “MOC”) for immediate visibility.  
- **CDN bypass pattern**: if fetching dataset assets for hub context, use `resolve/main/` URLs to avoid HF API rate limits.  
- **Lightning/compute**: frontend-only; no training/compute invoked.  

Ship this card (~90 minutes) to immediately surface the highest-value hub signal on the Costinel dashboard.
