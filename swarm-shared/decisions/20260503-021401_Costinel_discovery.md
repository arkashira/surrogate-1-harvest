# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal/most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first static payload; zero backend changes; ships in <2h.

---

### 1) File changes (relative to `/opt/axentx/Costinel`)

- `src/components/dashboard/TopHubSignalPanel.tsx` (new) — React panel  
- `src/lib/topHubData.json` (new) — CDN-style static payload (committed to repo)  
- `src/components/dashboard/Dashboard.tsx` — import & mount panel  
- `src/styles/components/_top-hub-signal.scss` — minimal styling  

---

### 2) Data contract (CDN-first)

`src/lib/topHubData.json`

```json
{
  "hub": {
    "id": "MOC",
    "label": "MOC",
    "description": "Mission Operations Cost — highest-signal hub for cloud cost governance",
    "connectedCount": 42,
    "score": 98,
    "lastUpdated": "2026-05-03T02:12:03Z"
  },
  "proposals": [
    {
      "id": "P-20260503-001",
      "title": "Right-size over-provisioned EKS node groups",
      "impact": "HIGH",
      "estimatedMonthlySavingsUSD": 18400,
      "effort": "LOW",
      "risk": "low",
      "owner": "platform-team",
      "due": "2026-05-31",
      "tags": ["k8s", "autoscaling", "rightsizing"],
      "rationale": "CPU avg 22% across 3 node groups; enable cluster-autoscaler + Karpenter spot profiles."
    },
    {
      "id": "P-20260503-002",
      "title": "Convert steady-state RDS to Reserved Instances (1yr No Upfront)",
      "impact": "MEDIUM",
      "estimatedMonthlySavingsUSD": 9200,
      "effort": "LOW",
      "risk": "low",
      "owner": "finance-ops",
      "due": "2026-05-24",
      "tags": ["rds", "ri", "postgres"],
      "rationale": "78% steady-state utilization; RI coverage currently 34%."
    },
    {
      "id": "P-20260503-003",
      "title": "S3 Intelligent-Tiering for cold logs >30d",
      "impact": "MEDIUM",
      "estimatedMonthlySavingsUSD": 4100,
      "effort": "LOW",
      "risk": "none",
      "owner": "platform-team",
      "due": "2026-05-17",
      "tags": ["s3", "lifecycle", "storage"],
      "rationale": "2.1TB of audit/ingest logs on STANDARD; lifecycle rules absent."
    }
  ]
}
```

---

### 3) Component: `TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState } from "react";
import "./TopHubSignalPanel.scss";

const DATA_URL = `${process.env.PUBLIC_URL}/lib/topHubData.json`;

interface Proposal {
  id: string;
  title: string;
  impact: "HIGH" | "MEDIUM" | "LOW";
  estimatedMonthlySavingsUSD: number;
  effort: "LOW" | "MEDIUM" | "HIGH";
  risk: "low" | "medium" | "high" | "none";
  owner: string;
  due: string;
  tags: string[];
  rationale: string;
}

interface Hub {
  id: string;
  label: string;
  description: string;
  connectedCount: number;
  score: number;
  lastUpdated: string;
}

interface TopHubData {
  hub: Hub;
  proposals: Proposal[];
}

const riskColor = (risk: string) => {
  switch (risk?.toLowerCase()) {
    case "high":
      return "var(--risk-high, #ef4444)";
    case "medium":
      return "var(--risk-medium, #f59e0b)";
    default:
      return "var(--risk-low, #10b981)";
  }
};

const formatUSD = (n: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
  }).format(n);

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<Hub | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(DATA_URL, { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load hub signal: ${res.status}`);
        return res.json() as Promise<TopHubData>;
      })
      .then((data) => {
        setHub(data.hub);
        setProposals(data.proposals.slice(0, 3));
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        Loading hub signal…
      </div>
    );
  }

  if (error) {
    return (
      <div className="top-hub-panel error" role="alert">
        {error}
      </div>
    );
  }

  if (!hub) return null;

  return (
    <section className="top-hub-panel" aria-labelledby="hub-title">
      <header className="top-hub-header">
        <div>
          <h2 id="hub-title">{hub.label}</h2>
          <p className="hub-tag">{hub.id}</p>
        </div>
        <div className="hub-score" title="Signal score">
          {hub.score}
        </div>
      </header>

      <p className="hub-description">{hub.description}</p>

      <div className="proposals-list" aria-label="Top proposals">
        {proposals.map((p) => (
          <article key={p.id} className="proposal-card">
            <h3 className="proposal-title">{p.title}</h3>
            <p className="proposal-rationale">{p.rationale}</p>
            <div className="proposal-meta">
              <span className="impact">
                {p.impact} impact · {formatUSD(p.estimatedMonthlySavingsUSD)}/mo
              </span>
              <span
                className="risk-badge"
                style={{ backgroundColor: riskColor(p.risk) }}
              >
                {p.risk?.toLowerCase() || "low"}
              </span>
            </div>
            <div className="proposal-footer">
              <span className="owner">{p.owner}</span>
              <time dateTime={p.due}>{p.due}</time>
            </div>
          </article>
        ))}
      </div>

      <footer className="panel-footer">
        <small>Last updated: {new Date(hub.lastUpdated).toLocaleString()}</small>
      </footer>
    </section>
  );
}
```

---

### 4) Styles: `_top-hub-signal.scss`

```scss
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 10px;
  padding: 
