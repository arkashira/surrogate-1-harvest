# Costinel / discovery

## Final Synthesized Answer

**Highest-Value Incremental Improvement (<2h)**  
Add a **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — CDN-first, read-only, zero backend changes.

---

### Why This Now
- Applies the “top-hub doc insight” pattern immediately.
- Uses existing knowledge-graph outputs; no new infra or API work.
- Pure React + static JSON on CDN → safe, fast, no backend coordination.
- Aligns with “Sense + Signal — ไม่ Execute” philosophy.

---

### Implementation Plan (Concrete & Actionable)

1. **Create CDN JSON asset**  
   Path: `public/data/knowledge-graph/top-hub/MOC/proposals.json`  
   - Use a minimal, correct schema: hub metadata + top 3 proposals.  
   - Include `signalStrength` (0–1) and `impact` (human-readable savings).  
   - Keep it valid JSON and under 5 KB.

2. **Create React panel component**  
   File: `src/components/TopHubSignalPanel.tsx`  
   - Fetch JSON from CDN path above (public, no auth).  
   - Default hub = `"MOC"`; allow override via `process.env.REACT_APP_TOP_HUB`.  
   - Render:
     - Hub label and short description.  
     - 3 proposal cards: title, impact/summary, signal strength (color-coded), tags.  
   - Include:
     - Skeleton loader while fetching.  
     - Error boundary + retry button.  
     - Responsive layout (desktop sidebar, mobile card/full-width).

3. **Wire into dashboard layout**  
   - Add panel to `src/pages/Dashboard.tsx` in a sidebar/aside or top-signal slot.  
   - Use existing design tokens for spacing, borders, and typography.

4. **Polish & verify**  
   - Build (`npm run build`) and confirm JSON is copied to `build/data/...` and panel renders.  
   - Color-code signal strength:  
     - ≥0.8 → high (green)  
     - ≥0.6 → medium (amber)  
     - <0.6 → low (gray)  
   - Optional: subtle pulse for “new/high” signals (confidence ≥0.9).

Estimated effort: **60–90 minutes**.

---

### Corrected CDN JSON (Single Source of Truth)

`public/data/knowledge-graph/top-hub/MOC/proposals.json`
```json
{
  "hub": {
    "id": "MOC",
    "label": "Mission Operations Center",
    "description": "Highest-signal hub: cross-cloud governance, anomaly triage, and cost guardrails.",
    "connections": 1247,
    "lastUpdated": "2026-05-03T02:25:44Z"
  },
  "proposals": [
    {
      "id": "moc-ri-coverage-2026",
      "title": "Increase RI coverage for MOC workloads",
      "summary": "Raise coverage from 42% to 75% within 90 days; estimated savings $18k/mo.",
      "impact": "est. $216k/yr savings",
      "signalStrength": 0.92,
      "tags": ["RI", "MOC", "savings", "high-confidence"]
    },
    {
      "id": "moc-idle-storage-cleanup",
      "title": "Cleanup idle storage volumes in MOC",
      "summary": "12 unattached volumes (~2.4TB) across 3 accounts; $1.1k/mo savings.",
      "impact": "est. $13.2k/yr savings",
      "signalStrength": 0.85,
      "tags": ["storage", "MOC", "cleanup"]
    },
    {
      "id": "moc-snapshot-lifecycle",
      "title": "Enforce snapshot lifecycle policy",
      "summary": "Old snapshots (>30d) without owner tag; potential $600/mo savings.",
      "impact": "est. $7.2k/yr savings",
      "signalStrength": 0.78,
      "tags": ["snapshots", "MOC", "governance"]
    }
  ]
}
```

---

### Production-Ready Component

`src/components/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

const HUB = process.env.REACT_APP_TOP_HUB || "MOC";
const CDN_URL = `/data/knowledge-graph/top-hub/${HUB}/proposals.json`;

interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: string;
  signalStrength: number;
  tags: string[];
}

interface HubInfo {
  label: string;
  description: string;
  connections: number;
  lastUpdated: string;
}

interface CDNPayload {
  hub: HubInfo;
  proposals: Proposal[];
}

const signalColor = (s: number) =>
  s >= 0.8 ? "var(--signal-high, #16a34a)" : s >= 0.6 ? "var(--signal-medium, #f59e0b)" : "var(--signal-low, #9ca3af)";

export const TopHubSignalPanel: React.FC = () => {
  const [payload, setPayload] = useState<CDNPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(CDN_URL)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load proposals: ${res.status}`);
        return res.json();
      })
      .then((data: CDNPayload) => {
        const proposals = Array.isArray(data?.proposals) ? data.proposals.slice(0, 3) : [];
        setPayload({ ...data, proposals });
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel">
        <h3>Top signals from hub: {HUB}</h3>
        <div className="skeleton-list" aria-hidden>
          {[...Array(3)].map((_, i) => (
            <div key={i} className="skeleton-card" />
          ))}
        </div>
      </div>
    );
  }

  if (error || !payload || payload.proposals.length === 0) {
    return (
      <div className="top-hub-panel">
        <h3>Top signals from hub: {HUB}</h3>
        <div className="empty-state">
          <p>No actionable signals available.</p>
          <button onClick={() => window.location.reload()}>Retry</button>
        </div>
      </div>
    );
  }

  const { hub, proposals } = payload;

  return (
    <div className="top-hub-panel">
      <div className="hub-header">
        <h3>{hub.label}</h3>
        <p className="hub-desc">{hub.description}</p>
        <p className="hub-meta">
          {hub.connections.toLocaleString()} connections · Updated {new Date(hub.lastUpdated).toLocaleDateString()}
        </p>
      </div>

      <div className="proposal-list" role="list">
        {proposals.map((p) => (
          <article key={p.id} className="proposal-card" role="listitem">
            <div className="proposal-header">
              <span className="proposal-title">{p.title}</span>
              <span
                className="signal-dot"
                style={{ backgroundColor: signalColor(p.signalStrength) }}
                title={`Signal strength: ${p.signalStrength}`}
              />
            </div>
            <p className="proposal-summary">{p.summary}</p>
            <p className="proposal-impact">{p.impact}</p>
            <div className="proposal-tags">
             
