# Costinel / frontend

## Final Synthesized Implementation (Best Parts + Corrected Contradictions)

I have synthesized the strongest elements from both proposals. 

**Resolution of Contradictions:**
*   **Data Structure:** Candidate 1 uses a flat `top-hub-moc.json`; Candidate 2 uses a nested `hubs/MOC.json` with a `title` and `description`. **Winner: Candidate 2.** It is more scalable (allows adding more hubs later) and provides better UI context.
*   **Fetching Strategy:** Candidate 1 suggests runtime fallback to a local file; Candidate 2 implies strict build-time embedding. **Winner: Hybrid.** Use Candidate 1's robust runtime CDN fetch (with 5-min cache) but point it at Candidate 2's nested path. 
*   **Actionability:** Candidate 1 provides complete, copy-pasteable code. **Winner: Candidate 1.** We will keep Candidate 1's flawless utility and component logic, but adapt it to Candidate 2's superior data schema.

Here is the final, concrete, actionable plan to ship in <2 hours.

---

### 1. Data Architecture (The "Correct" Way)
Create a scalable, nested JSON structure. This resolves Candidate 2's fragmented snippet into a complete, actionable file.

**File Path:** `/opt/axentx/Costinel/public/data/hubs/MOC.json`
```json
{
  "hub": "MOC",
  "title": "Multi-Org Cost Governance",
  "description": "Top hub for cross-account cost anomalies and RI coverage.",
  "updatedAt": "2026-05-03T02:45:00Z",
  "proposals": [
    {
      "id": "P1",
      "title": "Shift 20% on-demand to 1-yr No-Upfront RIs",
      "impact": "high",
      "rationale": "Covers steady baseline load; ~30% cost reduction with minimal risk."
    },
    {
      "id": "P2",
      "title": "Delete unattached EBS volumes (>7d idle)",
      "impact": "medium",
      "rationale": "Frees $1.2k/mo orphaned storage; safe after snapshot verification."
    },
    {
      "id": "P3",
      "title": "Right-size over-provisioned m5.2xlarge nodes",
      "impact": "medium",
      "rationale": "CPU <35% for 14d; move to m5.xlarge saves ~$800/mo."
    }
  ]
}
```

### 2. CDN-Safe Fetcher Utility (Zero API Calls / Rate Limit Safe)
Adapted from Candidate 1, updated to fetch the nested path from Candidate 2. This ensures zero auth headers and bypasses HuggingFace rate limits via CDN.

**File Path:** `src/lib/signals/useTopHubSignals.js`
```js
// CDN-first, zero-auth, rate-limit-safe
const HUB = "MOC"; // Default top hub
const CDN_URL = `https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/public/data/hubs/${HUB}.json`;
const CACHE_TTL_MS = 5 * 60 * 1000; // 5m client cache

async function fetchTopHubSignals(controller) {
  try {
    const res = await fetch(CDN_URL, {
      signal: controller?.signal,
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    return await res.json();
  } catch (err) {
    if (err.name === "AbortError") throw err;
    // Fallback to locally bundled copy (embedded at build time)
    const localRes = await fetch(`/data/hubs/${HUB}.json`, { cache: "no-store" });
    if (!localRes.ok) throw new Error("Both CDN and local signals unavailable");
    return await localRes.json();
  }
}

let cached = null;
let cachedAt = 0;

export function useTopHubSignals(suspense = false) {
  const [data, setData] = React.useState(() => {
    if (cached && Date.now() - cachedAt < CACHE_TTL_MS) return cached;
    return null;
  });
  const [loading, setLoading] = React.useState(!data);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    if (cached && Date.now() - cachedAt < CACHE_TTL_MS) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    fetchTopHubSignals(controller)
      .then((json) => {
        cached = json;
        cachedAt = Date.now();
        setData(json);
        setError(null);
      })
      .catch((err) => {
        if (err.name !== "AbortError") setError(err.message);
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, []);

  if (suspense && loading) throw new Promise(() => {});
  return { data, loading, error };
}
```

### 3. Top-Hub Signal Panel (UI / Read-Only)
Merges Candidate 1's flawless UI logic with Candidate 2's richer data schema (displaying `title` and `description`). Strictly follows the *Sense + Signal — ไม่ Execute* philosophy.

**File Path:** `src/components/TopHubSignalPanel.jsx`
```jsx
import React from "react";
import { useTopHubSignals } from "../lib/signals/useTopHubSignals";

const impactColors = {
  high: "bg-red-100 text-red-800 border-red-200",
  medium: "bg-amber-100 text-amber-800 border-amber-200",
  low: "bg-blue-100 text-blue-800 border-blue-200",
};

export default function TopHubSignalPanel() {
  const { data, loading, error } = useTopHubSignals();

  return (
    <section className="rounded-xl border bg-white p-5 shadow-sm">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-900">Top-Hub Signals</h3>
          <p className="text-sm text-gray-500">Sense + Signal — ไม่ Execute</p>
        </div>
      </div>

      {loading && (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg bg-gray-100" />
          ))}
        </div>
      )}

      {error && (
        <div className="rounded-lg bg-yellow-50 p-3 text-sm text-yellow-800">
          Signals unavailable — panel will retry on next load.
        </div>
      )}

      {data && (
        <>
          <div className="mb-4">
            <h4 className="font-bold text-gray-900">{data.title}</h4>
            <p className="text-xs text-gray-500">{data.description}</p>
          </div>

          <div className="mb-3 flex items-center gap-2 text-xs text-gray-400">
            <span className="font-mono">Hub: {data.hub}</span>
            <span>•</span>
            <span>Updated {new Date(data.updatedAt).toLocaleDateString()}</span>
          </div>

          <div className="space-y-3">
            {data.proposals.slice(0, 3).map((p) => (
              <article
                key={p.id}
                className="rounded-lg border p-3 transition-colors hover:bg-gray-50"
              >
                <div className="mb-2 flex items-start justify-between gap-2">
                  <h4 className="text-sm font-semibold text-gray-900">{p.title}</h4>
                  <span
                    className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium border ${
                      impactColors[p.impact] || impactColors.low

