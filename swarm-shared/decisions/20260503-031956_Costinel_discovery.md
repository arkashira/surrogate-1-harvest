# Costinel / discovery

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected hub (default `MOC`) from a lightweight hub-graph index
- Shows 3 contextual insights from knowledge-rag
- Uses CDN-first pattern to avoid HF API rate limits during data fetch
- Zero backend changes — pure frontend fetch + local JSON index

---

### 1. File layout (existing repo assumptions)
```
/opt/axentx/Costinel/
├── public/
│   ├── data/
│   │   └── hub-index.json          # lightweight hub graph (committed)
│   └── insights/
│       └── moc/                    # per-hub insights (CDN-ready)
│           └── 2026-05-03.json
├── src/
│   └── components/
│       └── TopHubSignalPanel.jsx   # new component
└── README.md
```

---

### 2. Data contract (committed once, updated by ops)

`public/data/hub-index.json`
```json
{
  "generatedAt": "2026-05-03T03:13:56Z",
  "topHub": "MOC",
  "hubs": {
    "MOC": {
      "name": "Mission Operations Center",
      "degree": 142,
      "insightPath": "/insights/moc/2026-05-03.json",
      "tags": ["knowledge-rag", "graph", "hub"]
    },
    "SEC": {
      "name": "Security Command",
      "degree": 98,
      "insightPath": "/insights/sec/2026-05-03.json"
    }
  }
}
```

`public/insights/moc/2026-05-03.json`
```json
{
  "hub": "MOC",
  "date": "2026-05-03",
  "insights": [
    {
      "rank": 1,
      "title": "Costinel MOC cross-links to 142 services",
      "snippet": "Highest betweenness centrality — primary coordination node for cost governance signals.",
      "tags": ["knowledge-rag", "graph", "hub"]
    },
    {
      "rank": 2,
      "title": "Anomaly cluster around MOC egress spikes",
      "snippet": "Detected 3σ egress cost surge in APAC region; recommend reserved capacity review.",
      "tags": ["anomaly", "egress", "forecast"]
    },
    {
      "rank": 3,
      "title": "Orphaned resource density near MOC",
      "snippet": "12 unattached volumes and 7 idle NAT gateways within MOC-linked accounts.",
      "tags": ["governance", "cleanup", "ri-coverage"]
    }
  ]
}
```

---

### 3. Component implementation (frontend-only)

`src/components/TopHubSignalPanel.jsx`
```jsx
import { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

export default function TopHubSignalPanel() {
  const [panel, setPanel] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let mounted = true;

    async function load() {
      try {
        // CDN-first: no auth header, bypasses HF API rate limits
        const indexRes = await fetch("/data/hub-index.json", { cache: "no-store" });
        if (!indexRes.ok) throw new Error("Hub index unavailable");
        const index = await indexRes.json();

        const topKey = index.topHub || Object.keys(index.hubs)[0];
        const hubMeta = index.hubs[topKey];

        const insightRes = await fetch(hubMeta.insightPath, { cache: "no-store" });
        if (!insightRes.ok) throw new Error("Insights unavailable");
        const insightData = await insightRes.json();

        if (!mounted) return;

        setPanel({ hubMeta, insightData });
      } catch (err) {
        if (!mounted) return;
        setError(err.message);
      } finally {
        if (!mounted) return;
        setLoading(false);
      }
    }

    load();
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <span>Loading signals…</span>
      </div>
    );
  }

  if (error) {
    // Non-blocking: silently degrade
    console.warn("TopHubSignalPanel:", error);
    return null;
  }

  const { hubMeta, insightData } = panel;

  return (
    <section className="top-hub-panel" aria-label={`Top hub: ${hubMeta.name}`}>
      <header className="top-hub-header">
        <div>
          <h3>{hubMeta.name}</h3>
          <span className="top-hub-badge">Top hub</span>
        </div>
        <span className="top-hub-degree">{hubMeta.degree} connections</span>
      </header>

      <ul className="top-hub-insights" role="list">
        {insightData.insights.map((ins) => (
          <li key={ins.rank} className="top-hub-insight">
            <div className="insight-rank">{ins.rank}</div>
            <div className="insight-body">
              <div className="insight-title">{ins.title}</div>
              <div className="insight-snippet">{ins.snippet}</div>
              <div className="insight-tags">
                {ins.tags.map((t) => (
                  <span key={t} className="insight-tag">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          </li>
        ))}
      </ul>

      <footer className="top-hub-footer">
        <small>
          Updated {new Date(insightData.date).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
            year: "numeric",
          })}
        </small>
      </footer>
    </section>
  );
}
```

`src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 10px;
  padding: 16px;
  background: #fff;
  max-width: 520px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
    Arial, sans-serif;
}

.top-hub-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

.top-hub-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: #111827;
}

.top-hub-badge {
  display: inline-block;
  margin-left: 8px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  color: #0ea5e9;
  background: #e0f2fe;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.top-hub-degree {
  font-size: 13px;
  color: #6b7280;
  white-space: nowrap;
}

.top-hub-insights {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}


