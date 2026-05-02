# Costinel / frontend

## Final Synthesis — Costinel Frontend: Top-Hub Insight Panel (≤2h)

**Chosen approach**: Combine Candidate 1’s fast, deterministic UI + auditability with Candidate 2’s use of the real `discovery/manifest-{env}.json` and live polling.  
**Result**: A production-ready, frontend-only panel that is correct, actionable, and future-proof.

---

### Core Decisions (resolve contradictions)

| Contradiction | Resolution (correctness + actionability) |
|--------------|-------------------------------------------|
| Static JSON (`top-hubs.json`) vs. existing `discovery/manifest-{env}.json` | Use `discovery/manifest-{env}.json`. It already exists per backend plan; avoids dual sources of truth. Keep a minimal fallback shape for dev when missing. |
| No polling vs. 60s polling | Add polling (60s) while dashboard is visible; pause on page hidden (requestIdle/visibility API). Keeps data fresh without wasteful background traffic. |
| “Refresh” button simulates fetch vs. real fetch | Keep button for manual refresh; wire it to the same fetch path as polling. Remove “simulated” require/import. |
| Rank/tie-break logic only vs. connection count + signals | Deterministic rank + connection count + short rationale (top 3 signals). Show connection count and up to 3 signals for immediate context. |
| Provenance: generatedAt only vs. generatedAt + lastRefresh | Show both: source timestamp (`generatedAt`) and local last-refresh time. Improves auditability and UX. |

---

### Implementation Plan (≤2h)

1. **Create a small data adapter**  
   - Path: `src/data/adapters/hubInsightsAdapter.ts`  
   - Reads `discovery/manifest-{env}.json` (public path).  
   - Normalizes to `TopHubBundle` (same shape as Candidate 1 + connection count + signals).  
   - Deterministic sort: `connections desc`, tie-break `hubId asc`.  
   - Graceful fallback: if file missing or empty, render nothing (no errors).

2. **Build `TopHubInsight` component**  
   - Fetches manifest JSON from `/discovery/manifest-{env}.json` (public).  
   - Polling: 60s interval while page visible; clears on unmount/hidden.  
   - Manual “Refresh” button triggers same fetch.  
   - Shows:
     - Hub name + connection count
     - Short insight (configurable text or derived)
     - Up to 3 top signals (short labels)
     - “View details” link to signals page filtered by hub
     - Provenance: source `generatedAt` + local `lastRefresh`
   - Accessibility: aria labels, keyboard focus, semantic markup.

3. **Mount into dashboard**  
   - Place in existing sidebar or right-rail on `/dashboard`.  
   - Keep width constrained (≈320px) to avoid layout shift.

4. **Styling & tokens**  
   - Reuse existing design tokens and card styles.  
   - Ensure responsive behavior (stack or hide on narrow screens if needed).

5. **Dev fallback**  
   - Provide `public/discovery/manifest-example.json` for local dev when backend manifest isn’t present.

---

### Data contract (normalized)

```ts
// src/types/hubInsights.ts
export interface HubRanking {
  hubId: string;
  label: string;
  rank?: number;
  connections: number;
  insight: string;
  signals: string[]; // short labels
  docsPath: string;
  signalsPagePath: string; // e.g. /signals?hub=MOC
}

export interface TopHubBundle {
  generatedAt: string;
  source: string;
  ranking: HubRanking[];
}
```

Adapter output must match this shape.

---

### Code — TopHubInsight component (final)

```tsx
// src/components/TopHubInsight.tsx
import React, { useEffect, useState, useCallback } from "react";
import type { TopHubBundle, HubRanking } from "../types/hubInsights";
import { getEnvManifestUrl, normalizeManifest } from "../data/adapters/hubInsightsAdapter";

const POLL_MS = 60_000;

const TopHubInsight: React.FC = () => {
  const [bundle, setBundle] = useState<TopHubBundle | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string>(new Date().toISOString());
  const [loading, setLoading] = useState<boolean>(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const data = await normalizeManifest();
      setBundle(data);
      setLastRefresh(new Date().toISOString());
    } catch (err) {
      // graceful: keep previous bundle or null; no console spam
      if (process.env.NODE_ENV === "development") {
        // eslint-disable-next-line no-console
        console.warn("TopHubInsight fetch failed", err);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData(); // initial

    const handleVisibility = () => {
      if (document.visibilityState === "visible") fetchData();
    };

    document.addEventListener("visibilitychange", handleVisibility);

    const interval = setInterval(() => {
      if (document.visibilityState === "visible") fetchData();
    }, POLL_MS);

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [fetchData]);

  const top: HubRanking | null = bundle?.ranking?.[0] ?? null;

  if (!top) {
    // Graceful fallback: render nothing when no data
    return null;
  }

  return (
    <aside
      className="top-hub-insight"
      aria-label={`Top hub insight: ${top.label}`}
      style={{
        border: "1px solid var(--border-color, #e5e7eb)",
        borderRadius: "8px",
        padding: "16px",
        background: "var(--bg-elevated, #fff)",
        maxWidth: "320px",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h3 style={{ margin: "0 0 4px", fontSize: "14px", fontWeight: 600 }}>
            Top Hub: {top.label}
          </h3>
          <p style={{ margin: "0 0 4px", fontSize: "13px", color: "var(--text-muted, #6b7280)" }}>
            {top.connections} connections
          </p>
          <p style={{ margin: "0 0 8px", fontSize: "13px", color: "var(--text-muted, #6b7280)" }}>
            {top.insight}
          </p>

          {top.signals && top.signals.length > 0 && (
            <ul style={{ margin: "0 0 8px", paddingLeft: "16px", fontSize: "12px", color: "var(--text-muted, #6b7280)" }}>
              {top.signals.slice(0, 3).map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          )}

          <div style={{ fontSize: "11px", color: "var(--text-muted, #6b7280)" }}>
            <div>Source: {bundle?.source ?? "manifest"}</div>
            <div>Updated: {bundle ? new Date(bundle.generatedAt).toLocaleString() : "—"}</div>
            <div>Refreshed: {new Date(lastRefresh).toLocaleString()}</div>
          </div>
        </div>
      </div>

      <div style={{ marginTop: "12px", display: "flex", gap: "8px", flexWrap: "wrap" }}>
        <a
          href={top.docsPath}
          style={{
            fontSize: "13px",
            padding: "6px 10px",
            borderRadius: "6px",
            background: "var(--accent-bg, #eff6ff)",
            color: "var(--ac
