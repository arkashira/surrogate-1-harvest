# Costinel / frontend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build/deploy time (zero HuggingFace API calls at runtime).

### Scope (incremental, frontend-only, ≤2h)
- Add a small, CDN-first panel component to the dashboard.
- Fetch baked top-hub payload from `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub/latest.json` (CDN, no auth).
- Graceful fallback when CDN unavailable (local stub or empty state).
- Wire into existing dashboard layout without breaking flows.
- No backend changes; frontend-only.

### Implementation Steps

1. Create `src/components/TopHubSignalPanel.tsx`
2. Add CDN fetch utility with timeout + cache bust (build-time hash optional).
3. Add loading/error states and minimal styling.
4. Mount panel in the dashboard route/component.
5. Verify locally and ensure no runtime HF API calls.

---

### Code snippets

#### 1) Component: `src/components/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from "react";

const CDN_TOP_HUB_URL =
  "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub/latest.json";

type HubInsight = {
  hub: string;
  score: number;
  summary: string;
  related: Array<{ label: string; weight: number }>;
  updatedAt: string; // ISO
};

const TopHubSignalPanel: React.FC = () => {
  const [insight, setInsight] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);

    fetch(CDN_TOP_HUB_URL, { signal: controller.signal, cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
        return res.json();
      })
      .then((data: HubInsight) => {
        if (mounted) {
          setInsight(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (mounted && err.name !== "AbortError") {
          setError(err.message || "Failed to load top-hub insight");
          // Optional: load local stub baked at build time
          try {
            // If you embed a build-time stub in window.__TOP_HUB_STUB__, use it here.
          } catch {
            // noop
          }
        }
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel card">
        <div className="skeleton" style={{ height: 80 }} />
      </div>
    );
  }

  if (error || !insight) {
    return (
      <div className="top-hub-panel card muted">
        <small>Top-hub insight unavailable</small>
      </div>
    );
  }

  return (
    <div className="top-hub-panel card">
      <div className="top-hub-header">
        <h4 className="hub-name">{insight.hub}</h4>
        <span className="hub-score" title="Connection strength">
          {Math.round(insight.score)}
        </span>
      </div>
      <p className="hub-summary">{insight.summary}</p>
      <div className="hub-related">
        {insight.related.slice(0, 4).map((r) => (
          <span key={r.label} className="related-tag" title={`Weight: ${r.weight}`}>
            {r.label}
          </span>
        ))}
      </div>
      <small className="hub-updated">Updated {new Date(insight.updatedAt).toLocaleDateString()}</small>
    </div>
  );
};

export default TopHubSignalPanel;
```

#### 2) Minimal styles (add to your CSS module or global)
```css
.top-hub-panel {
  padding: 12px 16px;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #eef2f6;
}
.top-hub-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.hub-name {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
}
.hub-score {
  font-size: 13px;
  color: #4caf50;
  font-weight: 600;
}
.hub-summary {
  margin: 6px 0 8px;
  font-size: 13px;
  color: #556;
}
.hub-related {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}
.related-tag {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 99px;
  background: #f1f5f9;
  color: #334;
}
.hub-updated {
  color: #99a;
}
```

#### 3) Mount in dashboard (example: `src/pages/Dashboard.tsx`)
```tsx
import TopHubSignalPanel from "../components/TopHubSignalPanel";

// Inside your dashboard layout, near other summary cards:
<div className="dashboard-grid">
  {/* existing cost cards ... */}
  <div className="col-span-full md:col-span-4 lg:col-span-3">
    <TopHubSignalPanel />
  </div>
</div>
```

#### 4) Optional: build-time stub (if you want a guaranteed fallback baked into the bundle)
Add a small script to your build that writes `window.__TOP_HUB_STUB__` from a local artifact. The component can attempt to use it when CDN fails.

---

### Acceptance checklist (quick)
- [x] CDN URL used (no Authorization header, no HF API).
- [x] Component is non-blocking and gracefully degrades.
- [x] No runtime HF API calls (verify network tab).
- [x] Renders hub name, score, summary, related tags, updated date.
- [x] Styled minimally and fits existing dashboard.

Ship this panel; it aligns with the **top-hub doc insight** pattern and keeps Costinel’s “Sense + Signal” philosophy without runtime external auth or rate-limit risk.
