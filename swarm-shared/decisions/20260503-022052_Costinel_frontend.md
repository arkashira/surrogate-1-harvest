# Costinel / frontend

### Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. Uses a CDN-first data path (no backend calls), typed data contract, caching, and a concise, actionable UI. Ships in **<2 hours**.

---

### 1) Data contract (CDN JSON)

Create `/public/data/top-hub-signals.json` (committed to repo; served via CDN; zero auth/rate-limit):

```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "description": "Cross-cloud governance hub for incident-to-cost linkage and change proposals.",
  "updatedAt": "2026-05-03T02:14:42Z",
  "proposals": [
    {
      "id": "moc-ri-2026-05",
      "title": "RI Coverage Gap: us-east-1 prod nodes",
      "impact": "high",
      "signal": "37% RI under-utilization; $18.4k/mo savings",
      "actions": ["Purchase 1-yr convertible RI", "Tag owners for approval"]
    },
    {
      "id": "moc-snap-2026-05",
      "title": "EBS snapshot retention drift",
      "impact": "medium",
      "signal": "140 stale snapshots (~$2.1k/mo)",
      "actions": ["Apply snapshot TTL policy", "Notify backup owners"]
    },
    {
      "id": "moc-idle-2026-05",
      "title": "Idle dev clusters nights/weekends",
      "impact": "medium",
      "signal": "22% avg CPU nights; $5.6k/mo",
      "actions": ["Enable cluster auto-suspend", "Create opt-in schedule"]
    }
  ]
}
```

---

### 2) Component: `TopHubSignalPanel.tsx`

Create `src/components/TopHubSignalPanel.tsx`:

```tsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

type Proposal = {
  id: string;
  title: string;
  impact: 'high' | 'medium' | 'low';
  signal: string;
  actions: string[];
};

type TopHubData = {
  hub: string;
  title: string;
  description: string;
  updatedAt: string;
  proposals: Proposal[];
};

const CDN_URL = `${process.env.PUBLIC_URL}/data/top-hub-signals.json`;

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(CDN_URL, { cache: 'force-cache' })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load hub signals: ${res.status}`);
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

  if (loading) return <div className="top-hub-panel loading">Loading…</div>;
  if (error) return <div className="top-hub-panel error">Error: {error}</div>;
  if (!data) return null;

  return (
    <aside className="top-hub-panel" aria-label={`Top hub: ${data.title}`}>
      <header className="top-hub-header">
        <h2 className="top-hub-title">{data.title}</h2>
        <p className="top-hub-sub">{data.description}</p>
        <time className="top-hub-updated" dateTime={data.updatedAt}>
          Updated {new Date(data.updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
        </time>
      </header>

      <section className="top-hub-proposals" aria-label="Top proposals">
        {data.proposals.map((p) => (
          <article key={p.id} className={`proposal-card impact-${p.impact}`}>
            <div className="proposal-header">
              <span className={`impact-badge impact-${p.impact}`}>{p.impact}</span>
              <h3 className="proposal-title">{p.title}</h3>
            </div>
            <p className="proposal-signal">{p.signal}</p>
            <ul className="proposal-actions">
              {p.actions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </article>
        ))}
      </section>
    </aside>
  );
}
```

---

### 3) Styles: `TopHubSignalPanel.css`

Create `src/components/TopHubSignalPanel.css`:

```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  max-width: 420px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

.top-hub-header { margin-bottom: 12px; }
.top-hub-title { margin: 0 0 4px; font-size: 18px; }
.top-hub-sub { margin: 0 0 6px; color: #556; font-size: 13px; }
.top-hub-updated { margin: 0; color: #889; font-size: 12px; }

.top-hub-proposals { display: flex; flex-direction: column; gap: 10px; }

.proposal-card {
  border: 1px solid #eef0f3;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fbfdff;
}

.proposal-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
.proposal-title { margin: 0; font-size: 14px; font-weight: 600; }
.proposal-signal { margin: 0 0 6px; color: #223; font-size: 13px; }

.proposal-actions { margin: 0; padding-left: 16px; color: #345; font-size: 12px; }
.proposal-actions li { margin-bottom: 2px; }

.impact-badge {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: #fff;
}

.impact-high { background: #b91c1c; }
.impact-medium { background: #d97706; }
.impact-low { background: #4b5563; }

.loading, .error { padding: 14px; color: #666; font-size: 13px; }
.error { color: #b91c1c; }
```

---

### 4) Integration & caching strategy

- **CDN-first**: JSON is committed to `/public/data/` and served statically (or via CDN). No backend required.
- **Caching**: `fetch(..., { cache: 'force-cache' })` leverages browser HTTP cache. Optionally add `react-query` later if you need stale-while-revalidate or background refetching.
- **Fallback**: If the CDN file is missing or malformed, show a clear error in the panel (no crash).
- **Type safety**: The TypeScript contract ensures future graph-to-CDN transforms preserve required fields.

---

### 5) Implementation Steps (actionable)

1. Add `/public/data/
