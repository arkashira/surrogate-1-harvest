# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: Frontend-only card that identifies the most-connected hub from the knowledge-rag graph and surfaces the top hub with 3 contextual signals.  
**Timebox**: ≤2h.  
**Pattern**: Sense + Signal (read-only; no execution).

---

### 1) Highest-value incremental improvement
Embed a **Top-Hub Signal Card** into the Costinel dashboard that:
- Reads a lightweight, cached knowledge-rag graph snapshot (JSON) produced by prior research runs.
- Computes the most-connected hub (highest degree) client-side.
- Renders the hub name + 3 contextual signals (short summaries or doc links).
- Updates automatically when the snapshot changes (poll or manual refresh).

This delivers immediate “Sense + Signal” value without backend changes and aligns with the “Review most-connected hub before planning” pattern.

---

### 2) Concrete implementation steps (≤2h)

**Step 1: Create graph snapshot contract** (5 min)  
Path: `/public/knowledge-rag/top-hub-snapshot.json`  
Schema:
```json
{
  "generatedAt": "2026-05-03T00:00:00Z",
  "hubs": [
    {
      "id": "MOC",
      "label": "MOC",
      "degree": 42,
      "type": "hub",
      "signals": [
        { "title": "Q2 cloud run-rate spike", "summary": "Linked to MOC burst workloads; 18% above forecast.", "doc": "/docs/moc-q2-run-rate.md" },
        { "title": "Reserved Instance gap", "summary": "MOC shows 35% RI coverage shortfall for steady-state nodes.", "doc": "/docs/moc-ri-gap.md" },
        { "title": "Anomalous egress pattern", "summary": "Cross-region egress tied to MOC replication jobs detected 2d ago.", "doc": "/docs/moc-egress-anomaly.md" }
      ]
    }
  ]
}
```

**Step 2: Add card component** (45 min)  
File: `src/components/cards/TopHubSignalCard.tsx` (React).  
Responsibilities:
- Fetch snapshot from `/knowledge-rag/top-hub-snapshot.json`.
- Pick top hub by `degree`.
- Render card with hub label, degree, and 3 signals.
- Graceful fallback if snapshot missing.

```tsx
import React, { useState, useEffect } from 'react';

interface Signal {
  title: string;
  summary: string;
  doc?: string;
}

interface Hub {
  id: string;
  label: string;
  degree: number;
  type: string;
  signals: Signal[];
}

interface Snapshot {
  generatedAt: string;
  hubs: Hub[];
}

const TopHubSignalCard: React.FC = () => {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const topHub = snapshot?.hubs.reduce((a, b) => (a.degree > b.degree ? a : b)) || null;

  const fetchSnapshot = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/knowledge-rag/top-hub-snapshot.json', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: Snapshot = await res.json();
      setSnapshot(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSnapshot();
  }, []);

  const formatDate = (iso: string) => new Date(iso).toLocaleString();

  if (loading) return <div className="loading">Loading signals…</div>;
  if (error) return <div className="error">Signals unavailable.</div>;
  if (!topHub) return <div className="error">No hub data available.</div>;

  return (
    <div className="top-hub-card">
      <header>
        <h3>Top Hub</h3>
        <span className="hub-name">{topHub.label}</span>
        <span className="hub-degree">{topHub.degree} connections</span>
      </header>
      <section className="signals">
        {topHub.signals.map((s, i) => (
          <article key={i} className="signal">
            <h4>{s.title}</h4>
            <p>{s.summary}</p>
            {s.doc && (
              <a href={s.doc} className="doc-link" target="_blank" rel="noopener noreferrer">
                Read more →
              </a>
            )}
          </article>
        ))}
      </section>
      <footer className="meta">
        Updated {formatDate(snapshot!.generatedAt)}
        <button onClick={fetchSnapshot} className="refresh-btn">Refresh</button>
      </footer>
    </div>
  );
};

export default TopHubSignalCard;
```

Styles (CSS or Tailwind):
```css
.top-hub-card { padding: 1rem; border: 1px solid #e5e7eb; border-radius: 8px; max-width: 520px; }
.hub-name { font-weight: 700; font-size: 1.25rem; }
.hub-degree { color: #6b7280; font-size: 0.875rem; margin-left: 0.5rem; }
.signals { margin-top: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }
.signal h4 { margin: 0 0 0.25rem; font-size: 0.95rem; }
.signal p { margin: 0 0 0.25rem; color: #374151; font-size: 0.875rem; }
.doc-link { font-size: 0.8125rem; color: #2563eb; text-decoration: none; }
.meta { margin-top: 0.75rem; font-size: 0.75rem; color: #6b7280; display: flex; justify-content: space-between; align-items: center; }
.refresh-btn { font-size: 0.75rem; padding: 0.25rem 0.5rem; cursor: pointer; }
```

**Step 3: Wire into dashboard** (15 min)  
Place `<TopHubSignalCard />` in the dashboard sidebar or top summary row. Ensure responsive layout.

**Step 4: Add snapshot generator script (dev-only)** (30 min)  
File: `scripts/generate-top-hub-snapshot.js` (run by research pipeline).  
- Reads local knowledge-rag graph export (or queries a local API).
- Computes degrees and picks top hub + 3 signals.
- Writes `/public/knowledge-rag/top-hub-snapshot.json`.

```js
// scripts/generate-top-hub-snapshot.js
const fs = require('fs');
const path = require('path');

function computeTopHub() {
  // In production, load from exported graph JSON
  // const graph = JSON.parse(fs.readFileSync('exports/knowledge-rag-graph.json'));
  // const degrees = {};
  // graph.links.forEach(l => { degrees[l.source] = (degrees[l.source]||0)+1; degrees[l.target] = (degrees[l.target]||0)+1; });
  // const topId = Object.entries(degrees).sort((a,b)=>b[1]-a[1])[0][0];

  // Mocked result matching pattern
  return {
    generatedAt: new Date().toISOString(),
    hubs: [
      {
        id: 'MOC',
        label: 'MOC',
        degree: 42,
        type: 'hub',
        signals: [
          { title: 'Q2 cloud run-rate spike', summary: 'Linked to MOC burst workloads; 18% above forecast.', doc: '/docs/moc-q2-run-rate.md' },
          { title
