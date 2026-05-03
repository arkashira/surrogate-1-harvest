# Costinel / backend

Candidate 3:
## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

### Highest-value incremental improvement
Add a resilient, zero-runtime-HF-API “Top-Hub Signal” panel to Costinel that surfaces the most-connected hub (e.g., “MOC”) with baked CDN data, robust fallback UI, and no API-rate exposure.

### Why this now
- Past patterns show we must avoid runtime HF API (rate-limit 429, commit caps).
- CDN bypass (`resolve/main/`) is free of auth and rate limits.
- Top-hub insight (MOC) is high-signal for governance context.
- Pure frontend change → ships in <2h, no infra/training pipeline required.

---

## Concrete implementation plan

1. **Add baked data file**  
   Create `public/data/top-hub.json` with CDN-resident snapshot (updated by ops via CI, not runtime).  
   Schema: `{ "hub": "MOC", "connections": 142, "updatedAt": "2026-05-03T04:00:00Z", "insight": "Most-connected hub; prioritize policy templates and anomaly thresholds here." }`

2. **Create reusable hook for CDN fetch with fallback**  
   Add `src/hooks/useTopHubSignal.ts` — fetches `/data/top-hub.json`, caches in `localStorage` (stale-while-revalidate), returns safe defaults on failure.

3. **Add TopHubSignalPanel component**  
   Place in `src/components/TopHubSignalPanel.tsx` — card in cost dashboard showing hub, connections, recency, and insight. Skeleton while loading; inline error if unavailable.

4. **Wire into dashboard**  
   Import and mount in the main dashboard layout (likely `src/pages/Dashboard.tsx` or equivalent) near cost summary cards.

5. **Add tests & types**  
   Minimal TypeScript types and one unit test for the hook’s fallback behavior.

6. **Verify CDN path**  
   Ensure `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/public/data/top-hub.json` is reachable and CORS-enabled.

7. **Ops script (optional)**  
   Add `scripts/sync-top-hub.sh` to regenerate and push the baked file via HF dataset repo (one-time HF token at build time).

---

## Code snippets

### 1) Baked data file (public/data/top-hub.json)
```json
{
  "hub": "MOC",
  "connections": 142,
  "updatedAt": "2026-05-03T04:00:00Z",
  "insight": "Most-connected hub; prioritize policy templates and anomaly thresholds here."
}
```

### 2) Hook (src/hooks/useTopHubSignal.ts)
```ts
import { useEffect, useState } from "react";

const CDN_URL =
  "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/public/data/top-hub.json";
const LOCAL_PATH = "/data/top-hub.json";
const FALLBACK = {
  hub: "MOC",
  connections: 0,
  updatedAt: new Date().toISOString(),
  insight: "Signal unavailable",
};

export interface TopHubSignal {
  hub: string;
  connections: number;
  updatedAt: string;
  insight: string;
}

export default function useTopHubSignal() {
  const [data, setData] = useState<TopHubSignal>(FALLBACK);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchFrom = async (url: string) => {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error("CDN fetch failed");
      return res.json();
    };

    const load = async () => {
      try {
        // Try CDN first
        const cdnData = await fetchFrom(CDN_URL);
        setData(cdnData);
        try {
          localStorage.setItem("topHubSignal", JSON.stringify(cdnData));
        } catch (e) {
          /* ignore storage errors */
        }
      } catch (err) {
        // Fallback to local bundled file
        try {
          const localData = await fetchFrom(LOCAL_PATH);
          setData(localData);
        } catch (err2) {
          // Use cached or fallback
          try {
            const cached = localStorage.getItem("topHubSignal");
            if (cached) setData(JSON.parse(cached));
          } catch (e) {
            setData(FALLBACK);
          }
        }
      } finally {
        setLoading(false);
      }
    };

    load();
  }, []);

  return { data, loading };
}
```

### 3) Panel component (src/components/TopHubSignalPanel.tsx)
```tsx
import React from "react";
import useTopHubSignal from "../hooks/useTopHubSignal";

export default function TopHubSignalPanel() {
  const { data, loading } = useTopHubSignal();

  if (loading) {
    return (
      <div className="p-4 border rounded bg-white shadow-sm animate-pulse">
        <div className="h-4 w-24 bg-gray-200 rounded mb-2"></div>
        <div className="h-6 w-32 bg-gray-200 rounded mb-1"></div>
        <div className="h-3 w-full bg-gray-100 rounded"></div>
      </div>
    );
  }

  return (
    <div className="p-4 border rounded-lg bg-white shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <span className="text-xs font-medium text-gray-500 uppercase">Top Hub</span>
          <h3 className="text-lg font-semibold">{data.hub}</h3>
        </div>
        <span className="px-2 py-1 text-xs font-medium bg-blue-100 text-blue-800 rounded">
          {data.connections} links
        </span>
      </div>
      <p className="mt-2 text-sm text-gray-600">{data.insight}</p>
      <p className="mt-2 text-xs text-gray-400">Updated {new Date(data.updatedAt).toLocaleDateString()}</p>
    </div>
  );
}
```

### 4) Dashboard wiring (example)
```tsx
// src/pages/Dashboard.tsx
import TopHubSignalPanel from "../components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div className="p-6 space-y-6">
      {/* existing cost cards */}
      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        <TopHubSignalPanel />
        {/* other cards */}
      </div>
    </div>
  );
}
```

### 5) Ops script (scripts/sync-top-hub.sh)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Generate baked top-hub.json from knowledge-rag (pseudo)
# knowledge-rag --query "top hub" --output public/data/top-hub.json

# Example manual fallback:
mkdir -p public/data
cat > public/data/top-hub.json <<'EOF'
{
  "hub": "MOC",
  "connections": 142,
  "updatedAt": "2026-05-03T04:00:00Z",
  "insight": "Most-connected hub; prioritize policy templates and anomaly thresholds here."
}
EOF

# Commit and push to HF dataset repo (one-time HF token at build time)
# git add public/data/top-hub.json
# git commit -m "chore: update top-hub baked data"
# git push
```

---

## Final answer (synthesized)

**Goal**: Add a resilient, zero-runtime-HF-API “Top-Hub Signal” panel to Costinel that surfaces the most-connected hub (e.g., “MOC”) using baked CDN data, robust fallback UI, and no API-rate exposure. Ship in <2h.

**Chosen approach (synthesis)**:
- Use a **backend endpoint** (`/api/v1
