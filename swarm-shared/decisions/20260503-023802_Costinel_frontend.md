# Costinel / frontend

## Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **zero Hugging Face API calls during render**, fully CDN-backed, and ready for human review.

---

## Implementation Plan (≤2h)

1. **Static data bundle** (5–10m)  
   - Create `/public/data/top-hub-moc.json` with `{ hub, proposals: [{ title, impact, signal, cdnPath }] }`  
   - Use CDN paths only (`/data/...`), no runtime API.

2. **UI component** (45–60m)  
   - Add `TopHubSignalPanel` React component (TypeScript) to `src/components/dashboard/`.  
   - Fetch static JSON via `fetch('/data/top-hub-moc.json')` (cached, CDN-backed).  
   - Render hub header + 3 proposal cards with impact badges and “Review” CTA.

3. **Dashboard integration** (20–30m)  
   - Mount `TopHubSignalPanel` near the top of the main dashboard view (`src/pages/Dashboard.tsx`).  
   - Ensure responsive layout (grid/flex) and visible priority styling.

4. **Styling + polish** (15–20m)  
   - Use existing design tokens (colors, spacing).  
   - Add subtle entrance animation and loading/error states.

5. **Build + smoke test** (10–15m)  
   - `npm run build` and verify `/data/top-hub-moc.json` is served and panel renders with no runtime API calls.

---

## Code Snippets

### 1) Static CDN-backed data (`/public/data/top-hub-moc.json`)

```json
{
  "hub": "MOC",
  "updated": "2026-05-03T02:40:00Z",
  "proposals": [
    {
      "id": "moc-ri-001",
      "title": "Convert 32x m5.large to 3-yr RI (us-east-1)",
      "impact": "HIGH",
      "signal": "38% coverage gap; $42k/yr savings",
      "cdnPath": "/data/proposals/moc-ri-001.json"
    },
    {
      "id": "moc-snap-002",
      "title": "Orphaned EBS snapshot cleanup (prod accounts)",
      "impact": "MEDIUM",
      "signal": "1.2TB unused; $1.1k/mo savings",
      "cdnPath": "/data/proposals/moc-snap-002.json"
    },
    {
      "id": "moc-down-003",
      "title": "Right-size over-provisioned RDS (db.t3.large → db.t3.medium)",
      "impact": "MEDIUM",
      "signal": "Avg CPU 18%; $860/mo savings",
      "cdnPath": "/data/proposals/moc-down-003.json"
    }
  ]
}
```

### 2) React component (`src/components/dashboard/TopHubSignalPanel.tsx`)

```tsx
import { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

interface Proposal {
  id: string;
  title: string;
  impact: "HIGH" | "MEDIUM" | "LOW";
  signal: string;
  cdnPath: string;
}

interface TopHubData {
  hub: string;
  updated: string;
  proposals: Proposal[];
}

const impactColor = (impact: string) => {
  switch (impact) {
    case "HIGH":
      return "var(--impact-high, #ef4444)";
    case "MEDIUM":
      return "var(--impact-medium, #f59e0b)";
    default:
      return "var(--impact-low, #10b981)";
  }
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/data/top-hub-moc.json", { cache: "force-cache" })
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load hub signals");
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <div className="skeleton"></div>
        <div className="skeleton short"></div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="top-hub-panel error">
        Unable to load top hub signals.{" "}
        <button onClick={() => window.location.reload()}>Retry</button>
      </div>
    );
  }

  return (
    <section className="top-hub-panel" aria-label={`Top hub: ${data.hub}`}>
      <header className="top-hub-header">
        <h2>Top Hub: {data.hub}</h2>
        <time dateTime={data.updated} className="top-hub-updated">
          Updated {new Date(data.updated).toLocaleDateString()}
        </time>
      </header>

      <div className="top-hub-proposals" role="list">
        {data.proposals.map((p) => (
          <article key={p.id} className="proposal-card" role="listitem">
            <div className="proposal-main">
              <h3 className="proposal-title">{p.title}</h3>
              <p className="proposal-signal">{p.signal}</p>
            </div>
            <div className="proposal-meta">
              <span
                className="impact-badge"
                style={{ backgroundColor: impactColor(p.impact) }}
              >
                {p.impact}
              </span>
              <a
                href={p.cdnPath}
                className="review-cta"
                target="_blank"
                rel="noopener noreferrer"
              >
                Review
              </a>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
```

### 3) Basic styles (`src/components/dashboard/TopHubSignalPanel.css`)

```css
.top-hub-panel {
  margin-bottom: 1.25rem;
  padding: 1rem;
  border-radius: 8px;
  background: var(--panel-bg, #ffffff);
  border: 1px solid var(--border-color, #e5e7eb);
}

.top-hub-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.top-hub-header h2 {
  font-size: 1.125rem;
  margin: 0;
}

.top-hub-updated {
  font-size: 0.875rem;
  color: #6b7280;
}

.top-hub-proposals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.75rem;
}

.proposal-card {
  padding: 0.875rem;
  border-radius: 6px;
  background: var(--card-bg, #f9fafb);
  border: 1px solid var(--border-color, #e5e7eb);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 96px;
}

.proposal-main {
  flex: 1;
}

.proposal
