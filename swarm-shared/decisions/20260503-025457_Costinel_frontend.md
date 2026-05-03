# Costinel / frontend

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted on the Costinel dashboard (sidebar or top banner area).
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, top 3 actionable signals, last updated timestamp.
- CDN-first data fetch: uses `https://huggingface.co/datasets/.../resolve/main/hubs/{hubName}.json` to bypass HF API rate limits.
- Graceful fallback: if CDN fails, shows cached local stub and logs (no crash).
- Zero backend changes; pure frontend feature flag toggle.

### Why this is highest value (<2h)
- Applies the **top-hub doc insight (MOC)** pattern directly to the user-facing dashboard.
- Uses **HF CDN bypass** pattern to avoid rate limits and keep frontend self-contained.
- Delivers immediate contextual value to users without touching ingestion/training pipelines.
- Small, testable, and reversible.

---

### Implementation steps (1h 30m total)

1. **Add config constant** (5m)
   - Create `src/config/hubs.ts` with `HUB_NAME` default `"MOC"` and CDN base URL.

2. **Create signal panel component** (40m)
   - `src/components/TopHubSignalPanel.tsx`
   - Fetch hub JSON from CDN on mount (`useEffect`).
   - Shape: `{ title, description, signals: [{ label, value, trend, action }], updatedAt }`.
   - Render compact card with 3 signals list and timestamp.
   - Implement local stub fallback if CDN fails or shape invalid.

3. **Mount on dashboard** (20m)
   - Import and place `TopHubSignalPanel` in the dashboard layout (likely sidebar or top banner).
   - Ensure non-blocking: panel failure does not affect main dashboard.

4. **Styling & polish** (15m)
   - Use existing design tokens (colors, spacing).
   - Add subtle loading state and last-updated label.

5. **Test & verify** (10m)
   - Run dev server, confirm CDN fetch and rendering.
   - Simulate CDN failure (block request) to verify fallback.

---

### Code snippets

#### src/config/hubs.ts
```ts
export const HUB_NAME = "MOC";
export const HUB_CDN_BASE = "https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/hubs";
export const getHubCdnUrl = (hubName: string) => `${HUB_CDN_BASE}/${hubName}.json`;
```

#### src/components/TopHubSignalPanel.tsx
```tsx
import React, { useEffect, useState } from "react";
import { getHubCdnUrl, HUB_NAME } from "../config/hubs";

type Signal = {
  label: string;
  value?: string | number;
  trend?: "up" | "down" | "flat";
  action?: string;
};

type HubData = {
  title: string;
  description: string;
  signals: Signal[];
  updatedAt: string;
};

const STUB_HUB: HubData = {
  title: "MOC",
  description: "Multi-org cost signals and governance insights.",
  signals: [
    { label: "Idle resources", value: "12", trend: "down", action: "Review" },
    { label: "Unattached disks", value: "4", trend: "flat", action: "Clean up" },
    { label: "RI coverage", value: "68%", trend: "up", action: "Optimize" },
  ],
  updatedAt: new Date().toISOString(),
};

export const TopHubSignalPanel: React.FC = () => {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch(getHubCdnUrl(HUB_NAME), { cache: "no-store" });
        if (!res.ok) throw new Error("CDN fetch failed");
        const data = (await res.json()) as HubData;
        // Basic shape validation
        if (!data.title || !Array.isArray(data.signals)) throw new Error("Invalid shape");
        setHub(data);
      } catch (err) {
        console.warn("[TopHubSignalPanel] CDN fetch failed, using stub:", err);
        setHub(STUB_HUB);
      } finally {
        setLoading(false);
      }
    };

    load();
  }, []);

  if (loading) {
    return (
      <div className="p-3 border rounded bg-gray-50 text-sm text-gray-500">
        Loading hub signals...
      </div>
    );
  }

  if (!hub) return null;

  return (
    <div className="p-3 border rounded bg-white shadow-sm">
      <div className="flex items-start justify-between mb-2">
        <div>
          <h3 className="font-semibold text-gray-900">{hub.title}</h3>
          <p className="text-xs text-gray-500">{hub.description}</p>
        </div>
      </div>

      <ul className="space-y-1.5">
        {hub.signals.slice(0, 3).map((s, i) => (
          <li key={i} className="flex items-center justify-between text-sm">
            <span className="text-gray-700">{s.label}</span>
            <div className="flex items-center gap-2">
              {s.value !== undefined && (
                <span className="font-mono text-gray-900">{s.value}</span>
              )}
              {s.trend && (
                <span
                  className={`inline-block w-2 h-2 rounded-full ${
                    s.trend === "up"
                      ? "bg-red-500"
                      : s.trend === "down"
                      ? "bg-green-500"
                      : "bg-gray-400"
                  }`}
                  title={`trend: ${s.trend}`}
                />
              )}
              {s.action && (
                <button
                  className="text-xs text-blue-600 hover:underline"
                  onClick={() => console.log("Action:", s.action)}
                >
                  {s.action}
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>

      <div className="mt-2 text-xs text-gray-400">
        Updated {new Date(hub.updatedAt).toLocaleString()}
      </div>
    </div>
  );
};
```

#### Mount in dashboard (example)
```tsx
// In your dashboard layout component
import { TopHubSignalPanel } from "../components/TopHubSignalPanel";

// Place where appropriate (sidebar/top banner)
<TopHubSignalPanel />
```

---

### Acceptance criteria
- Panel appears on dashboard with MOC hub data from CDN (or stub if CDN unreachable).
- No console errors on failure; graceful fallback visible.
- Panel does not block dashboard interaction or main data loads.
- Configurable hub name via `HUB_NAME` in `src/config/hubs.ts`.

---

### Notes & follow-ups
- If CDN path or repo differs, update `HUB_CDN_BASE` accordingly.
- Consider adding a manual refresh button and caching strategy (e.g., localStorage with TTL) in a future iteration.
- If design system tokens differ, adjust class names to match existing theme.
