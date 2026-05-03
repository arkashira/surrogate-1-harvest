# Costinel / discovery

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HuggingFace API calls at runtime).

---

### 1) Build-time data pipeline (Mac/CI) — 15m
Create `scripts/build-top-hub.js` that:
- Runs once after rate-limit window clears (or via cron)
- Uses single `list_repo_tree` call for a date folder (or local knowledge-rag export)
- Produces `public/data/top-hub.json` with CDN-resolved assets (no auth)
- Commits the JSON so the web build includes it

```json
// public/data/top-hub.json (example)
{
  "hub": "MOC",
  "title": "MOC — Method of Choice",
  "score": 0.94,
  "connections": 128,
  "summary": "Most-connected hub for cost-governance patterns. Prefer CDN-first ingestion and Lightning reuse to preserve quota.",
  "related": [
    { "label": "Surrogate-1 schema fix", "href": "/docs/patterns/surrogate-1-schema" },
    { "label": "Lightning Studio reuse", "href": "/docs/patterns/lightning-quota" },
    { "label": "HF CDN bypass", "href": "/docs/patterns/hf-cdn-bypass" }
  ],
  "updatedAt": "2026-05-03T04:00:00.000Z"
}
```

Script sketch:
```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/axentx/Costinel
node scripts/build-top-hub.js
# outputs to public/data/top-hub.json
```

---

### 2) Frontend component — 45m
Add `src/components/TopHubSignalPanel.jsx` (or `.tsx`) that:
- Loads `public/data/top-hub.json` at runtime (static fetch, no auth)
- Non-blocking: lazy-load or low-priority fetch; graceful fallback if missing
- Renders a compact card with hub name, score, summary, and related links
- Uses existing design tokens (colors, spacing) from Costinel

```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

interface RelatedItem {
  label: string;
  href: string;
}

interface TopHubData {
  hub: string;
  title: string;
  score: number;
  connections: number;
  summary: string;
  related: RelatedItem[];
  updatedAt: string;
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Low-priority fetch; non-blocking
    const controller = new AbortController();
    fetch("/data/top-hub.json", { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((json) => setData(json))
      .catch(() => setData(null))
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, []);

  if (loading) return null; // non-blocking: render nothing while loading
  if (!data) return null; // fail silently if CDN file missing

  return (
    <aside className="top-hub-panel" aria-label="Top hub signal">
      <div className="top-hub-panel__header">
        <span className="top-hub-panel__badge">Top Hub</span>
        <span className="top-hub-panel__hub">{data.hub}</span>
        <span className="top-hub-panel__score" title="Connection score">
          {Math.round(data.score * 100)}%
        </span>
      </div>
      <p className="top-hub-panel__title">{data.title}</p>
      <p className="top-hub-panel__summary">{data.summary}</p>
      {data.related.length > 0 && (
        <ul className="top-hub-panel__related" aria-label="Related items">
          {data.related.map((item, idx) => (
            <li key={idx}>
              <a href={item.href} className="top-hub-panel__link">
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      )}
      <small className="top-hub-panel__meta">
        Updated {new Date(data.updatedAt).toLocaleDateString()}
      </small>
    </aside>
  );
}
```

Basic styles (`src/components/TopHubSignalPanel.css`):
```css
.top-hub-panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
  max-width: 320px;
  font-size: 13px;
  line-height: 1.4;
  color: #374151;
}
.top-hub-panel__header {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 6px;
}
.top-hub-panel__badge {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #6b7280;
}
.top-hub-panel__hub {
  font-weight: 700;
  color: #111827;
  flex: 1;
}
.top-hub-panel__score {
  font-variant-numeric: tabular-nums;
  color: #059669;
  font-weight: 600;
}
.top-hub-panel__title {
  margin: 4px 0 6px;
  font-weight: 600;
  color: #111827;
}
.top-hub-panel__summary {
  margin: 0 0 8px;
  color: #6b7280;
}
.top-hub-panel__related {
  list-style: none;
  padding: 0;
  margin: 0 0 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
}
.top-hub-panel__link {
  color: #2563eb;
  text-decoration: none;
  font-size: 12px;
}
.top-hub-panel__link:hover {
  text-decoration: underline;
}
.top-hub-panel__meta {
  color: #9ca3af;
}
```

---

### 3) Integration into dashboard — 20m
Place the panel in an existing dashboard view (e.g., cost overview sidebar or top bar). Example placement in `src/pages/Dashboard.jsx`:

```tsx
import TopHubSignalPanel from "../components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div className="dashboard">
      <header className="dashboard__header">
        <h1>Cloud Cost Governance</h1>
        {/* Top-hub signal — non-blocking */}
        <TopHubSignalPanel />
      </header>
      {/* rest of dashboard */}
    </div>
  );
}
```

If the design prefers a sidebar or card grid, drop it into the appropriate grid cell. Ensure it doesn’t block critical content (CSS: `order` or grid placement).

---

### 4) Build & deploy checklist — 10m
- Add `public/data/top-hub.json` to repo (generated by CI or initial seed)
- Ensure build pipeline runs `scripts/build-top-hub.js` before static build (or commit the JSON manually for now)
- Verify CDN path `/data/top-hub.json` is served (static file)
- Confirm no runtime HF API calls (network tab)
- Test graceful fallback when JSON missing or malformed

---

### 5) Optional automation (cron) — 10m
Add a cron job on Mac/CI to refresh
