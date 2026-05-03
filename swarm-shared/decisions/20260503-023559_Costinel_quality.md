# Costinel / quality

## Final Synthesis — Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — **CDN-first, rate-limit-safe, zero API calls during render**.

### Why this is highest value
- Directly applies the **top-hub doc insight** and **CDN bypass** patterns.
- Improves governance visibility (“Sense + Signal — ไม่ Execute”) with minimal surface.
- Can ship in <2h: static panel + CDN fetch + simple render.
- Read-only signal aligns with Costinel philosophy and avoids runtime rate limits.

---

## Implementation Plan (merged & hardened)

1. **Create a lightweight hub manifest** (one-time, Mac orchestration)
   - Path: `public/signals/top-hub-moc.json`  
     (Use `public/` so Next.js serves it as static; avoids SSR complexity and guarantees CDN-like behavior locally.)
   - Contains: `{ hub, title, description, updatedAt, signals: [{id, title, impact, context, cdnPath}] }`
   - Committed to repo so dashboard can reference it without API calls.

2. **Add CDN-first signal loader**
   - Utility: `lib/signals/loadTopHubSignals.ts`
   - Primary source: repo-relative path `/signals/top-hub-moc.json` (served from `public/`).
   - Optional remote mirror: `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/signals/top-hub-moc.json`
   - Fetch with `cache: 'no-store'` in SSR or `fetch` in client with graceful fallback. No Authorization header.

3. **Add Signal Panel component**
   - Location: `components/SignalPanel.tsx`
   - Server-side preferred (Next.js): fetch in page/component with `cache: 'no-store'` to stay fresh but rely on CDN caching.
   - Client-side fallback: use SWR or simple `useEffect` if you prefer static export or SPA behavior.
   - Render: hub name, updated time, 3 signal cards with impact badges and optional link to evidence.

4. **Wire into dashboard**
   - Insert `<SignalPanel />` below main cost summary.
   - Keep layout responsive and visually consistent with existing tokens.

5. **Styling & polish**
   - Use existing design tokens (colors, spacing).
   - Add subtle icon for “hub” and impact levels.
   - Silent fail: if signals unavailable, render nothing (read-only).

---

## Code Snippets (merged + corrected)

### 1) Hub manifest (committed to `public/signals/top-hub-moc.json`)
```json
{
  "hub": "MOC",
  "title": "Multi-Org Cost Governance",
  "description": "Top-connected hub for cross-account cost visibility and policy signals.",
  "updatedAt": "2026-05-03T02:30:00Z",
  "signals": [
    {
      "id": "moc-ri-coverage",
      "title": "RI Coverage Gap",
      "impact": "high",
      "context": "RI coverage 42% for m5.large; 12-month convertible RI saves ~$310/mo.",
      "cdnPath": "/signals/evidence/moc-ri-m5large-2026-05-03.json"
    },
    {
      "id": "moc-orphaned-ebs",
      "title": "Unattached EBS volumes in us-east-1",
      "impact": "high",
      "context": "3 unattached gp3 volumes (~$45/mo) tagged for dev environments older than 30 days.",
      "cdnPath": "/signals/evidence/moc-ebs-unattached-2026-05-03.json"
    },
    {
      "id": "moc-sagemaker-idle",
      "title": "Idle SageMaker Endpoints",
      "impact": "medium",
      "context": "2 idle endpoints in staging; estimated $120/mo savings if stopped outside business hours.",
      "cdnPath": "/signals/evidence/moc-sagemaker-idle-2026-05-03.json"
    }
  ]
}
```

### 2) Signal loader (optional utility)
```ts
// lib/signals/loadTopHubSignals.ts
export type Signal = {
  id: string;
  title: string;
  impact: "high" | "medium" | "low";
  context: string;
  cdnPath: string;
};

export type HubManifest = {
  hub: string;
  title: string;
  description: string;
  updatedAt: string;
  signals: Signal[];
};

const LOCAL_PATH = "/signals/top-hub-moc.json";
const REMOTE_MIRROR =
  "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/signals/top-hub-moc.json";

export async function loadTopHubSignals(
  options: { preferRemote?: boolean } = {}
): Promise<HubManifest | null> {
  const url = options.preferRemote ? REMOTE_MIRROR : LOCAL_PATH;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error("Failed to fetch signals");
    return res.json();
  } catch {
    // If remote fails and we were using remote, try local as fallback
    if (options.preferRemote) {
      try {
        const res = await fetch(LOCAL_PATH, { cache: "no-store" });
        if (!res.ok) return null;
        return res.json();
      } catch {
        return null;
      }
    }
    return null;
  }
}
```

### 3) SignalPanel component (Next.js, server-first)
```tsx
// components/SignalPanel.tsx
import { loadTopHubSignals } from "@/lib/signals/loadTopHubSignals";

const impactColor = {
  high: "bg-red-100 text-red-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-blue-100 text-blue-800",
};

export default async function SignalPanel() {
  const hub = await loadTopHubSignals({ preferRemote: false });

  if (!hub || !hub.signals?.length) {
    return null; // silent fail — read-only signal
  }

  return (
    <div className="p-4 border rounded-lg bg-white shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="font-semibold text-gray-900">{hub.title}</h3>
          <p className="text-sm text-gray-500">{hub.description}</p>
        </div>
        <span className="text-xs font-medium text-gray-400">HUB</span>
      </div>

      <ul className="space-y-2">
        {hub.signals.map((s) => (
          <li key={s.id} className="flex items-start gap-2 text-sm">
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium mt-0.5 ${impactColor[s.impact]}`}
            >
              {s.impact}
            </span>
            <div className="flex-1 min-w-0">
              <p className="text-gray-700 truncate">{s.title}</p>
              <p className="text-xs text-gray-500 mt-0.5">{s.context}</p>
            </div>
          </li>
        ))}
      </ul>

      <p className="mt-3 text-xs text-gray-400">
        Updated: {new Date(hub.updatedAt).toLocaleDateString()}
      </p>
    </div>
  );
}
```

### 4) Mount on dashboard (Next.js example)
```tsx
// app/dashboard/page.tsx  (or pages/dashboard.tsx)
import SignalPanel from "@/components/SignalPanel";

export default function Dashboard() {
  return (
    <main className="p-6 space-y-6">
      {/* Existing cost
