# Costinel / discovery

**Final Implementation — Costinel “Top-Hub Signal” Card (frontend-only, ≤2h, production-ready)**

---

## 1) Scope & constraints (non-negotiable)
- **Pure frontend** — zero backend, zero new APIs, no auth/infra changes.
- **Read-only** — Sense + Signal only.
- **Timeboxed to ≤2h** — prioritize working, graceful fallback over completeness.
- **Must degrade gracefully** when graph data is missing/unavailable (no broken UI).
- **Reusable, accessible, responsive** React component.

---

## 2) Deliverable
A single, reusable component:

```
src/components/Costinel/TopHubSignalCard.tsx
```

- Accepts optional `graphData` prop (nodes/edges).
- If no `graphData`, loads `src/data/knowledge-rag-sample.json` as fallback.
- Computes highest-degree hub.
- Displays **hub name + degree + 3 contextual signals** (summary + optional link).
- Renders compact, accessible UI with graceful fallback state.

Exported from `src/components/Costinel/index.ts` and dropped into an existing dashboard (or a minimal demo route).

---

## 3) File changes (minimal, safe)

- **Add**: `src/data/knowledge-rag-sample.json`
- **Add**: `src/components/Costinel/TopHubSignalCard.tsx`
- **Update**: `src/components/Costinel/index.ts` (export)
- **Update**: Add usage in an existing dashboard view (or a lightweight demo page).

---

## 4) Sample graph data

```json
// src/data/knowledge-rag-sample.json
{
  "nodes": [
    { "id": "MOC", "label": "MOC", "type": "hub" },
    { "id": "cost-forecast", "label": "Cost Forecast", "type": "signal" },
    { "id": "ri-coverage", "label": "RI Coverage", "type": "signal" },
    { "id": "anomaly-detection", "label": "Anomaly Detection", "type": "signal" },
    { "id": "aws", "label": "AWS", "type": "cloud" },
    { "id": "gcp", "label": "GCP", "type": "cloud" },
    { "id": "budgets", "label": "Budgets", "type": "process" },
    { "id": "audit-trail", "label": "Audit Trail", "type": "process" }
  ],
  "edges": [
    { "source": "MOC", "target": "cost-forecast", "weight": 8 },
    { "source": "MOC", "target": "ri-coverage", "weight": 7 },
    { "source": "MOC", "target": "anomaly-detection", "weight": 6 },
    { "source": "MOC", "target": "aws", "weight": 5 },
    { "source": "MOC", "target": "gcp", "weight": 4 },
    { "source": "cost-forecast", "target": "budgets", "weight": 3 },
    { "source": "ri-coverage", "target": "aws", "weight": 2 },
    { "source": "anomaly-detection", "target": "audit-trail", "weight": 2 }
  ]
}
```

---

## 5) Component implementation (TypeScript + React)

```tsx
// src/components/Costinel/TopHubSignalCard.tsx
import React, { useEffect, useMemo, useState } from "react";

export interface KnowledgeGraph {
  nodes: Array<{ id: string; label?: string; type?: string }>;
  edges: Array<{ source: string; target: string; weight?: number }>;
}

export interface Signal {
  id: string;
  label: string;
  summary: string;
  href?: string;
}

export interface TopHubSignalCardProps {
  /** Optional graph data. If omitted, loads sample JSON. */
  graphData?: KnowledgeGraph;
  /** Optional map from node id to signal summary text. */
  signalSummaries?: Record<string, string>;
  /** Max number of signals to display (default 3) */
  maxSignals?: number;
  className?: string;
}

const defaultSignalSummaries: Record<string, string> = {
  "cost-forecast":
    "ML-driven 30-day cost forecast with confidence intervals to plan budgets.",
  "ri-coverage":
    "Reserved Instance coverage analysis highlighting under-utilized commitments.",
  "anomaly-detection":
    "Real-time anomaly detection on spend spikes and idle resources.",
  aws: "Multi-account AWS cost allocation and tag compliance signals.",
  gcp: "GCP cost insights and recommender export signals.",
  budgets: "Budget threshold alerts and forecast vs actual signals.",
  "audit-trail": "Immutable audit trail for cost governance decisions.",
};

const fallbackSignals: Signal[] = [
  {
    id: "fallback-1",
    label: "Enable cost signals",
    summary: "Connect graph data to surface top hub signals.",
    href: undefined,
  },
  {
    id: "fallback-2",
    label: "Check data pipeline",
    summary: "Ensure knowledge-rag graph is populated and available.",
    href: undefined,
  },
  {
    id: "fallback-3",
    label: "Review documentation",
    summary: "See onboarding guide for graph ingestion steps.",
    href: "/docs/onboarding",
  },
];

const sampleDataPath = "/data/knowledge-rag-sample.json";

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  graphData,
  signalSummaries = defaultSignalSummaries,
  maxSignals = 3,
  className = "",
}) => {
  const [loadedGraph, setLoadedGraph] = useState<KnowledgeGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load sample JSON only if graphData not provided
  useEffect(() => {
    if (graphData) {
      setLoadedGraph(graphData);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(sampleDataPath)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load ${sampleDataPath}`);
        return res.json();
      })
      .then((json) => {
        if (!cancelled) setLoadedGraph(json);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Unknown error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [graphData]);

  // Compute top hub + signals
  const { topHub, signals } = useMemo(() => {
    if (!loadedGraph) return { topHub: null, signals: [] };

    const degree: Record<string, number> = {};
    for (const n of loadedGraph.nodes) degree[n.id] = 0;
    for (const e of loadedGraph.edges) {
      degree[e.source] = (degree[e.source] || 0) + 1;
      degree[e.target] = (degree[e.target] || 0) + 1;
    }

    const topId = Object.keys(degree).reduce((a, b) => (degree[a] > degree[b] ? a : b), loadedGraph.nodes[0]?.id || "");
    const topNode = loadedGraph.nodes.find((n) => n.id === topId) || null;

    // Build signals: pick connected nodes (by edges) as contextual signals
    const connectedIds = new Set<string>();
    for (const e of loadedGraph.edges) {
      if (e.source === topId) connectedIds.add(e.target);
      if (e.target === topId) connectedIds.add(e.source);
    }

    const candidates = Array.from(connectedIds)
      .map((id) => {
        const node = loadedGraph.nodes.find((n) => n.id === id);
        if (!node) return null;
        return {
          id: node.id,
          label: node.label || node.id,
          summary: signalSummaries[node.id
