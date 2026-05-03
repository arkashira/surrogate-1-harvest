# Costinel / quality

## Final Synthesized Implementation (Best of Both Candidates)

**Highest-Value Incremental Improvement (<2h)**

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — **CDN-first, rate-limit-safe, zero API calls during render**.

### Why This Is Highest Value
- **CDN-first architecture**: Uses HuggingFace dataset hosting as a static CDN to bypass API rate limits entirely; the UI never hits the HF API during render.
- **Read-only, non-breaking**: Fits Costinel philosophy (*Sense + Signal — ไม่ Execute*) with zero side effects.
- **Immediate visibility**: Surface high-impact savings directly on the dashboard without navigation.
- **<2h implementation**: One component + one JSON asset + one utility function.

---

### Implementation Plan

#### 1. Data Contract (5 min)
Create `public/signals/top-hub-moc.json` with this exact schema:

```json
{
  "hub": "MOC",
  "updated": "2026-05-03T02:30:00Z",
  "signals": [
    {
      "id": "ri-001",
      "title": "RI coverage gap in us-east-1",
      "impact": "high",
      "estimated_savings_usd": 12400,
      "description": "37% of m5.xlarge usage is on-demand; 1yr No Upfront RI reduces cost by 38%.",
      "action": "propose_ri_purchase",
      "tags": ["AWS", "Compute", "RI"]
    },
    {
      "id": "snap-002",
      "title": "Unattached EBS snapshot retention",
      "impact": "medium",
      "estimated_savings_usd": 3200,
      "description": "14 unattached snapshots older than 30 days; delete or archive to Glacier.",
      "action": "propose_snapshot_cleanup",
      "tags": ["AWS", "Storage"]
    },
    {
      "id": "idle-003",
      "title": "Idle RDS instances (dev)",
      "impact": "medium",
      "estimated_savings_usd": 2100,
      "description": "2 RDS instances <5% avg CPU over 14 days; schedule stop/start or downsize.",
      "action": "propose_rds_rightsize",
      "tags": ["AWS", "Database"]
    }
  ]
}
```

**Key details**:
- `updated`: ISO timestamp for cache-busting and freshness indication.
- `impact`: Only `low` | `medium` | `high` (strict union).
- `estimated_savings_usd`: Integer (no decimals for annual estimates).
- Exactly 3 signals to respect the “top 3” constraint.

---

#### 2. CDN-First Fetch Utility (15 min)
Create `src/lib/signals.js` with silent fallback to local JSON:

```js
export async function fetchTopHubSignals(hub = "MOC") {
  const url = `https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/public/signals/top-hub-${hub.toLowerCase()}.json`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`Failed to fetch signals: ${res.status}`);
    return await res.json();
  } catch (err) {
    console.warn("[Signals] CDN fetch failed, using local fallback", err);
    try {
      const local = await import(`../public/signals/top-hub-${hub.toLowerCase()}.json`);
      return local.default || local;
    } catch {
      return null;
    }
  }
}
```

**Why this works**:
- `cache: "no-store"` ensures fresh reads without stale CDN cache.
- No `Authorization` header required (public dataset).
- Silent degradation: if CDN fails, uses local bundled JSON; if that fails, returns `null` (panel hides gracefully).

---

#### 3. Signal Panel Component (30 min)
Create `src/components/SignalPanel.tsx`:

```tsx
import { useEffect, useState } from "react";
import { fetchTopHubSignals } from "../lib/signals";

interface Signal {
  id: string;
  title: string;
  impact: "low" | "medium" | "high";
  estimated_savings_usd: number;
  description: string;
  action: string;
  tags: string[];
}

interface TopHubData {
  hub: string;
  updated: string;
  signals: Signal[];
}

export default function SignalPanel({ hub = "MOC" }: { hub?: string }) {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetchTopHubSignals(hub)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [hub]);

  if (loading) return <div className="signal-panel loading">Loading signals…</div>;
  if (!data || !data.signals?.length) return null;

  const impactColor = (imp: string) =>
    ({ high: "#ef4444", medium: "#f59e0b", low: "#10b981" }[imp] || "#6b7280");

  return (
    <section className="signal-panel" aria-label={`Top hub: ${data.hub}`}>
      <header className="signal-panel__header">
        <h3>Top Hub: {data.hub}</h3>
        <time dateTime={data.updated} title={data.updated}>
          Updated {new Date(data.updated).toLocaleDateString()}
        </time>
      </header>

      <ul className="signal-panel__list">
        {data.signals.slice(0, 3).map((s) => (
          <li key={s.id} className="signal-card">
            <div className="signal-card__title">{s.title}</div>
            <div className="signal-card__meta">
              <span
                className="impact-badge"
                style={{ backgroundColor: impactColor(s.impact) }}
              >
                {s.impact}
              </span>
              <span className="savings">
                ${s.estimated_savings_usd.toLocaleString()}/yr
              </span>
            </div>
            <p className="signal-card__desc">{s.description}</p>
            <div className="signal-card__tags">
              {s.tags.map((t) => (
                <span key={t} className="tag">
                  {t}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>

      <style jsx>{`
        .signal-panel {
          border: 1px solid #e5e7eb;
          border-radius: 8px;
          padding: 16px;
          background: #fff;
          margin-bottom: 16px;
        }
        .signal-panel__header {
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          margin-bottom: 12px;
        }
        .signal-panel__header h3 {
          margin: 0;
          font-size: 1rem;
        }
        .signal-panel__list {
          list-style: none;
          padding: 0;
          margin: 0;
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .signal-card {
          padding: 12px;
          border: 1px solid #f3f4f6;
          border-radius: 6px;
          background: #fafafa;
        }
        .signal-card__title {
          font-weight: 600;
          margin-bottom: 6px;
        }
        .signal-card__meta {
          display: flex;
          gap: 8px;
          align-items: center;
          margin-bottom: 6px;
          font-size: 0.875rem;
