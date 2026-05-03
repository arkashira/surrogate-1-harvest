# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- Ships in <2h as a resilient, self-contained React component + optional lightweight API route.  
- Aligns with pattern: review most-connected hub before planning tasks (#knowledge-rag #graph #hub).  
- **Resilience-first**: graceful degradation to local mock data when graph/backend is unreachable.

---

### 1) File layout (add/modify)

```
Costinel/
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel/
│   │      ├─ TopHubSignalPanel.tsx
│   │      ├─ useTopHubSignal.ts
│   │      └─ TopHubSignalPanel.module.css
│   ├── lib/
│   │   └─ knowledgeGraph.ts          # optional: typed client for /api/top-hub
│   ├── pages/api/
│   │   └── top-hub.ts                # optional: Next.js API route (backend-agnostic)
│   └── app/(dashboard)/page.tsx      # compose panel into dashboard
└── public/
    └─ data/
       └─ top-hub-mock.json           # fallback when graph unavailable
```

---

### 2) API contract (backend-agnostic, optional)

GET `/api/top-hub?hub=MOC`

Success (200):
```json
{
  "hub": "MOC",
  "title": "MOC — Multi-Org Cost signals",
  "description": "Cross-account cost anomalies and RI coverage signals",
  "proposals": [
    {
      "id": "prop-001",
      "title": "RI coverage gap — us-east-1",
      "summary": "32% under-covered for m5 family; 14d forecast +$8.4k",
      "impact": "high",
      "actions": [
        { "label": "Review coverage", "href": "/reports/ri-coverage" },
        { "label": "Simulate RI", "href": "/tools/ri-simulator" }
      ]
    }
  ],
  "metrics": {
    "degree": 42,
    "lastUpdated": "2026-05-03T08:12:00.000Z"
  }
}
```

No data / not found (200 with empty):
```json
{ "hub": "MOC", "proposals": [], "metrics": null }
```

Error (5xx/4xx):
```json
{ "error": "message" }
```

---

### 3) Implementation

#### `public/data/top-hub-mock.json`
```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "summary": "Central coordination for cloud cost governance decisions and cross-team policy alignment.",
  "proposals": [
    {
      "id": "prop-001",
      "title": "Standardize RI purchase cadence",
      "description": "Shift ad-hoc RI buys to quarterly planning cycle to improve coverage and reduce waste.",
      "impact": "high",
      "tags": ["RI", "governance", "planning"],
      "actions": [
        { "label": "Review coverage report", "href": "/reports/ri-coverage" },
        { "label": "Open proposal", "href": "/proposals/prop-001" }
      ]
    },
    {
      "id": "prop-002",
      "title": "Tag enforcement for untagged resources",
      "description": "Apply mandatory cost-center tags; auto-generate signals for non-compliant resources.",
      "impact": "medium",
      "tags": ["tagging", "compliance", "signals"],
      "actions": [
        { "label": "View untagged", "href": "/inventory?untagged=true" },
        { "label": "Open proposal", "href": "/proposals/prop-002" }
      ]
    }
  ],
  "lastUpdated": "2026-05-03T02:00:00Z"
}
```

#### `src/lib/knowledgeGraph.ts` (optional typed client)
```ts
export type ProposalAction = { label: string; href: string };
export type Proposal = {
  id: string;
  title: string;
  summary?: string;
  description?: string;
  impact: "low" | "medium" | "high";
  tags?: string[];
  actions: ProposalAction[];
};

export type TopHubResponse = {
  hub: string;
  title: string;
  description?: string;
  summary?: string;
  proposals: Proposal[];
  metrics?: {
    degree: number;
    lastUpdated: string;
  } | null;
};

export async function fetchTopHub(
  hub = "MOC",
  signal?: AbortSignal
): Promise<TopHubResponse> {
  const res = await fetch(`/api/top-hub?hub=${encodeURIComponent(hub)}`, {
    cache: "no-store",
    signal,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `API error: ${res.status}`);
  }

  return res.json();
}
```

#### `src/components/TopHubSignalPanel/useTopHubSignal.ts`
```ts
import { useEffect, useState, useCallback } from "react";
import { fetchTopHub, type TopHubResponse } from "../../lib/knowledgeGraph";

const FALLBACK_URL = "/data/top-hub-mock.json";

export function useTopHubSignal() {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadFallback = useCallback(async () => {
    try {
      const res = await fetch(FALLBACK_URL, { cache: "no-store" });
      if (!res.ok) throw new Error("Fallback unavailable");
      const json = await res.json();
      setData(json);
    } catch {
      setError("Unable to load top-hub data.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const controller = new AbortController();

    fetchTopHub("MOC", controller.signal)
      .then((json) => {
        if (!cancelled) {
          setData(json);
          setLoading(false);
        }
      })
      .catch(() => {
        // graph/API failed — degrade gracefully to fallback
        if (!cancelled) loadFallback();
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [loadFallback]);

  return { data, loading, error };
}
```

#### `src/components/TopHubSignalPanel/TopHubSignalPanel.module.css`
```css
.panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  max-width: 560px;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.hubBadge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: #0b5cff;
  font-size: 14px;
}

.updated {
  font-size: 12px;
  color: #6b7280;
}

.summary {
  font-size: 13px;
  color: #374151;
  margin-bottom: 12px;
}

.proposals {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.proposal {
