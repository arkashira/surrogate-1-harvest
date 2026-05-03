# Costinel / backend

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (≤2h, frontend-only)

### Goal
- Frontend-only card that identifies the highest-degree hub from the knowledge-rag graph and surfaces 3 contextual, actionable signals.
- Zero backend changes, zero new APIs, zero infra/auth work.
- Must degrade gracefully when graph data is missing or malformed.

---

### Scope & Constraints (merged)
- Pure React/Next.js (client component).
- Read-only; no mutations, no backend.
- ≤2h timebox.
- Graceful empty/error/skeleton states required.
- Use existing graph export path: `/data/knowledge-rag/graph.json` (preferred) with local fallback sample in repo if needed for dev.

---

### Data contract (single source of truth)
Place sample or exported file at:  
`public/data/knowledge-rag/graph.json`

```json
{
  "generatedAt": "2026-05-03T04:45:00Z",
  "nodes": [
    { "id": "MOC", "label": "MOC", "type": "hub", "degree": 42, "category": "Cost Optimization" },
    { "id": "AWS", "label": "AWS", "type": "cloud", "degree": 28, "category": "Cloud" },
    { "id": "RI", "label": "Reserved Instances", "type": "topic", "degree": 19, "category": "Optimization" }
  ],
  "edges": [
    { "source": "MOC", "target": "AWS" },
    { "source": "MOC", "target": "RI" }
  ],
  "signals": [
    {
      "hubId": "MOC",
      "title": "Reserved Instance coverage 68% → 85%",
      "summary": "Potential savings $12.4k/mo by increasing RI coverage and modifying underutilized families.",
      "href": "https://docs.axentx/costinel/top-hub/moc-ri",
      "ts": "2026-05-02T14:03:00Z",
      "impactUSD": 12400
    },
    {
      "hubId": "MOC",
      "title": "Idle dev clusters (3) → stop schedule",
      "summary": "Apply scheduled stop/start or right-size; estimated savings $3.1k/mo.",
      "href": "https://docs.axentx/costinel/top-hub/moc-clusters",
      "ts": "2026-05-02T18:12:00Z",
      "impactUSD": 3100
    },
    {
      "hubId": "MOC",
      "title": "Cross-region egress spike (ap-southeast)",
      "summary": "Review VPC flow logs and consider VPC endpoints or TGW optimizations to reduce egress.",
      "href": "https://docs.axentx/costinel/top-hub/moc-egress",
      "ts": "2026-05-03T02:11:00Z",
      "impactUSD": null
    }
  ]
}
```

Notes:
- `degree` is primary ranking; if absent, compute from edges.
- `category` is optional display metadata.
- `signals` are pre-associated by `hubId` for fast frontend filtering.

---

### Component: `components/TopHubSignalCard.jsx`

```jsx
'use client';

import { useEffect, useState, useMemo } from 'react';

export default function TopHubSignalCard() {
  const [graph, setGraph] = useState(null);
  const [topHub, setTopHub] = useState(null);
  const [signals, setSignals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    async function load() {
      try {
        const res = await fetch('/data/knowledge-rag/graph.json', {
          cache: 'no-store',
        });
        if (!res.ok) throw new Error('Graph unavailable');
        const data = await res.json();

        // Validate minimal shape
        if (!Array.isArray(data?.nodes) || !Array.isArray(data?.signals)) {
          throw new Error('Invalid graph format');
        }

        setGraph(data);

        // Compute top hub: prefer degree; fallback to edge count
        const edges = Array.isArray(data.edges) ? data.edges : [];
        const hubCandidates = data.nodes.filter(
          (n) => n.type === 'hub' || n.type === 'topic' || !n.type
        );

        const degreeMap = new Map();
        hubCandidates.forEach((n) => {
          degreeMap.set(n.id, n.degree ?? 0);
        });

        // If degree missing/zero, compute from edges
        if (hubCandidates.some((n) => !n.degree)) {
          hubCandidates.forEach((n) => degreeMap.set(n.id, 0));
          edges.forEach((e) => {
            if (degreeMap.has(e.source)) degreeMap.set(e.source, degreeMap.get(e.source) + 1);
            if (degreeMap.has(e.target)) degreeMap.set(e.target, degreeMap.get(e.target) + 1);
          });
        }

        const best = hubCandidates.sort((a, b) => (degreeMap.get(b.id) || 0) - (degreeMap.get(a.id) || 0))[0];
        setTopHub(best || null);

        const hubSignals = best
          ? data.signals.filter((s) => s.hubId === best.id).slice(0, 3)
          : [];
        setSignals(hubSignals);
      } catch (err) {
        setError(err.message || 'Failed to load graph');
      } finally {
        setLoading(false);
      }
    }

    load();
  }, []);

  const totalPotential = useMemo(() => {
    return signals.reduce((sum, s) => sum + (Number(s.impactUSD) || 0), 0);
  }, [signals]);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4 shadow-sm">
        <div className="flex items-center justify-between mb-3">
          <div className="h-5 w-32 animate-pulse rounded bg-muted" />
          <div className="h-5 w-16 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-2">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-12 animate-pulse rounded bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  if (error || !topHub) {
    return (
      <div className="rounded-lg border bg-card p-4 shadow-sm">
        <p className="text-sm text-muted-foreground">
          {error || 'No hub data available'} — Sense + Signal will resume shortly.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-semibold">{topHub.label}</span>
            <span className="inline-flex items-center rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
              degree {topHub.degree || 0}
            </span>
          </div>
          {topHub.category && (
            <p className="text-xs text-muted-foreground mt-0.5">{topHub.category}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <a
            href={`/knowledge-rag/hubs/${encodeURIComponent(topHub.id)}`}
            className="text-xs text-muted-foreground hover:underline"
          >
            View graph
          </a>
          <button
            onClick={() => window.location.reload()}
            className="text-xs text-muted-foreground hover:underline"
            aria-label="Refresh card"

