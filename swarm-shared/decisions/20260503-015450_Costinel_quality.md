# Costinel / quality

## Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- CDN-first data delivery to bypass HF API limits and avoid runtime model loads.  
- Resilient, cache-first UX with graceful fallback and no backend changes.  
- Ships in <2h.

---

### 1) Data strategy (CDN-first, zero HF API at runtime)
- Pre-list files once from Mac (after rate-limit window) via `list_repo_tree(path, recursive=False)` for the date folder.  
- Save path list to `public/knowledge-rag/top-hub-index.json`.  
- At runtime, dashboard fetches only CDN URLs:  
  `https://huggingface.co/datasets/{repo}/resolve/main/knowledge-rag/top-hub/MOC.json`  
- Each hub file is a small, projection-ready JSON: `{ hub, score, proposals:[{id,title,signal,actions}] }`.

**File layout (commit to repo)**
```
public/
  knowledge-rag/
    top-hub-index.json          # ["MOC.json","IAM.json",...]
    top-hub/
      MOC.json
      IAM.json
```

---

### 2) Component: `TopHubSignalPanel`

**Location**: `src/components/TopHubSignalPanel.tsx` (or `.tsx` under `components/`).  
**Tech**: React, SWR (or `fetch` + `useEffect`), Tailwind (existing design tokens).  
**Props**: `defaultHub?: string` (defaults to `"MOC"`).

```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';

type Proposal = {
  id: string;
  title: string;
  signal: string;
  actions: string[];
};

type HubData = {
  hub: string;
  score: number;
  proposals: Proposal[];
};

export default function TopHubSignalPanel({ defaultHub = 'MOC' }: { defaultHub?: string }) {
  const [hubData, setHubData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();

    async function load() {
      try {
        // CDN path — no Authorization header required
        const res = await fetch(
          `https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/public/knowledge-rag/top-hub/${defaultHub}.json`,
          { signal: controller.signal, cache: 'force-cache' }
        );

        if (!res.ok) throw new Error(`Failed to load hub data: ${res.status}`);
        const json = (await res.json()) as HubData;
        if (mounted) {
          setHubData(json);
          setError(null);
        }
      } catch (err: any) {
        if (err.name !== 'AbortError' && mounted) {
          setError(err.message ?? 'Unknown error');
          setHubData(null);
        }
      } finally {
        if (mounted) setLoading(false);
      }
    }

    load();
    return () => {
      mounted = false;
      controller.abort();
    };
  }, [defaultHub]);

  if (loading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-200" />
        <div className="mt-3 space-y-2">
          <div className="h-4 w-full animate-pulse rounded bg-gray-100" />
          <div className="h-4 w-5/6 animate-pulse rounded bg-gray-100" />
        </div>
      </div>
    );
  }

  if (error || !hubData) {
    return (
      <div className="rounded-lg border border-yellow-100 bg-yellow-50 p-4 text-sm text-yellow-800 shadow-sm">
        Unable to load top-hub insights. Showing default guidance.
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-gray-900">Top Hub: {hubData.hub}</h3>
          <p className="text-xs text-gray-500">Relevance score: {hubData.score.toFixed(2)}</p>
        </div>
        <span className="inline-flex items-center rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
          {hubData.proposals.length} signal{hubData.proposals.length !== 1 ? 's' : ''}
        </span>
      </div>

      <div className="mt-4 space-y-3">
        {hubData.proposals.map((p) => (
          <div key={p.id} className="rounded border border-gray-100 bg-gray-50 p-3">
            <p className="text-sm font-medium text-gray-900">{p.title}</p>
            <p className="mt-1 text-xs text-gray-600">{p.signal}</p>
            {p.actions.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {p.actions.map((a, idx) => (
                  <span
                    key={idx}
                    className="rounded bg-white px-2 py-0.5 text-xs font-medium text-gray-700 shadow-xs ring-1 ring-inset ring-gray-200"
                  >
                    {a}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <p className="mt-3 text-xs text-gray-400">
        Sense + Signal — ไม่ Execute. Review proposals in change management.
      </p>
    </div>
  );
}
```

---

### 3) Dashboard integration

Add panel to the main dashboard grid (example placement).  
**File**: `src/pages/Dashboard.tsx` (or wherever the dashboard layout lives).

```tsx
// Inside your dashboard grid
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

// ...
<div className="grid gap-6 lg:grid-cols-3">
  <div className="lg:col-span-2">
    {/* existing cost charts/tables */}
  </div>
  <div className="lg:col-span-1">
    <TopHubSignalPanel defaultHub="MOC" />
  </div>
</div>
```

---

### 4) Data file templates (commit these)

`public/knowledge-rag/top-hub-index.json`
```json
["MOC.json", "IAM.json"]
```

`public/knowledge-rag/top-hub/MOC.json`
```json
{
  "hub": "MOC",
  "score": 0.94,
  "proposals": [
    {
      "id": "MOC-001",
      "title": "Reduce idle dev clusters in us-east-1",
      "signal": "Detected 14 idle EKS clusters (>72h no traffic) costing ~$320/mo.",
      "actions": ["Schedule stop (nights/weekends)", "Right-size node groups"]
    },
    {
      "id": "MOC-002",
      "title": "Convert gp2 volumes to gp3 for non-prod",
      "signal": "12 gp2 volumes eligible; estimated savings $85/mo with same performance.",
      "actions": ["Create change request", "Snapshot before migration"]
    }
  ]
}
```

---

### 5) Build & deploy checklist (≤30 min)

- [ ] Add component file (`TopHubSignalPanel.tsx`).  
- [ ] Add data files under `public/knowledge-rag
