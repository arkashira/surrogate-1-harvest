# Costinel / backend

**Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)**

---

### Scope & Value (merged)
- **Frontend-only, read-only React panel** that surfaces the highest-signal/most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph.
- **CDN-first data path** (static JSON) — no backend API, no auth changes, no infra risk, no latency regression.
- **Delivers immediate “Sense + Signal” visibility** on the Costinel dashboard without execution capability.
- **Ship target: <2 hours.**

---

### Why this is the highest-value incremental improvement (merged)
- Directly applies the validated **#knowledge-rag #hub** pattern and **#graph** insight already proven in Costinel context.
- Zero backend changes → no infra risk, no auth surface, no latency regression.
- Immediate UX payoff: decision-makers see the most-connected hub + proposals at a glance.
- Aligns with “Sense + Signal — ไม่ Execute” philosophy (read-only signals, no execution).
- Reuses existing component patterns (cards, badges, lists) for fast styling and integration.

---

### File changes (all in `/opt/axentx/Costinel`)

1. **`public/data/knowledge-hubs.json`** (new) — CDN-friendly static payload.  
2. **`src/components/dashboard/TopHubSignalPanel.tsx`** (new) — React panel component (TypeScript).  
3. **`src/components/dashboard/TopHubSignalPanel.module.css`** (new) — scoped styles.  
4. **`src/pages/Dashboard.tsx`** (modify) — mount panel near top of dashboard.

---

### 1) Static hub payload (CDN-first)

`public/data/knowledge-hubs.json`
```json
{
  "generatedAt": "2026-05-03T02:12:03Z",
  "highestSignalHub": {
    "id": "MOC",
    "name": "MOC",
    "title": "Multi-Org Cost governance",
    "description": "Mission Operations Cost governance hub — central node for cloud cost proposals, anomaly signals, and RI coverage recommendations.",
    "tags": ["#knowledge-rag", "#graph", "#hub"],
    "metrics": {
      "inDegree": 128,
      "outDegree": 94,
      "signalScore": 9.7
    },
    "proposals": [
      {
        "id": "P-MOC-001",
        "title": "RI coverage below 60% in prod accounts",
        "impact": "high",
        "signal": "Increase RI purchase for m6i.xlarge in us-east-1",
        "context": "3 accounts, 42% coverage, $18k/mo potential savings"
      },
      {
        "id": "P-MOC-002",
        "title": "Orphaned EBS volumes detected",
        "impact": "medium",
        "signal": "Delete unattached gp3 volumes older than 14 days",
        "context": "11 volumes, ~$220/mo"
      },
      {
        "id": "P-MOC-003",
        "title": "Idle node pools in non-prod",
        "impact": "medium",
        "signal": "Schedule stop/start for weekend idle clusters",
        "context": "3 GKE node pools, avg 68% weekend idle"
      }
    ]
  }
}
```

---

### 2) React panel component (TypeScript)

`src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from "react";
import styles from "./TopHubSignalPanel.module.css";

type Proposal = {
  id: string;
  title: string;
  impact: "high" | "medium" | "low";
  signal: string;
  context: string;
};

type HubData = {
  id: string;
  name: string;
  title: string;
  description: string;
  tags: string[];
  metrics: {
    inDegree: number;
    outDegree: number;
    signalScore: number;
  };
  proposals: Proposal[];
};

type KnowledgeHubsPayload = {
  generatedAt: string;
  highestSignalHub: HubData;
};

const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<HubData | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first fetch — no auth, no API gateway
    fetch("/data/knowledge-hubs.json", { cache: "no-cache" })
      .then((res) => res.json())
      .then((json: KnowledgeHubsPayload) => {
        setData(json.highestSignalHub);
        setUpdatedAt(json.generatedAt);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <section className={styles.panel} aria-busy="true">
        <div className={styles.skeletonTitle} />
        <div className={styles.skeletonList} />
      </section>
    );
  }

  if (!data) {
    return null; // Fail silent per read-only principle
  }

  const impactClass = (imp: string) => `${styles.impactBadge} ${styles[`impact${imp.charAt(0).toUpperCase() + imp.slice(1)}`]}`;

  return (
    <section className={styles.panel} aria-label={`Top hub: ${data.name}`}>
      <header className={styles.header}>
        <div className={styles.hubMeta}>
          <div className={styles.hubBadges}>
            <span className={styles.hubName}>{data.name}</span>
            {data.tags.slice(0, 2).map((tag) => (
              <span key={tag} className={styles.tag}>
                {tag}
              </span>
            ))}
          </div>
          <h3 className={styles.hubTitle}>{data.title}</h3>
          <p className={styles.hubDesc}>{data.description}</p>
          <div className={styles.hubMetrics}>
            <span>In: {data.metrics.inDegree}</span>
            <span>Out: {data.metrics.outDegree}</span>
            <span>Signal: {data.metrics.signalScore}</span>
          </div>
        </div>
        <time className={styles.updated} dateTime={updatedAt}>
          Updated {new Date(updatedAt).toLocaleDateString()}
        </time>
      </header>

      <ul className={styles.proposalsList} role="list">
        {data.proposals.map((p) => (
          <li key={p.id} className={styles.proposalCard}>
            <div className={styles.proposalHeader}>
              <span className={impactClass(p.impact)}>{p.impact}</span>
              <h4 className={styles.proposalTitle}>{p.title}</h4>
            </div>
            <p className={styles.proposalSignal}>{p.signal}</p>
            <p className={styles.proposalContext}>{p.context}</p>
          </li>
        ))}
      </ul>
    </section>
  );
};

export default TopHubSignalPanel;
```

---

### 3) Scoped styles (CSS Modules)

`src/components/dashboard/TopHubSignalPanel.module.css`
```css
.panel {
  border: 1px solid #e6e9ee;
  border-radius: 10px;
  padding: 18px 20px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 14px;
}

.hubMeta {
  flex: 1;
  min-width: 0;
}

.hubBadges {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom:
