# Costinel / backend

## Final Implementation Plan — Costinel Top-Hub Signal Card

**Chosen approach**: Pure frontend React + TypeScript. No backend/auth changes.  
**Scope**: Add a “Top-Hub Insight” card to the dashboard that surfaces the most-connected hub (e.g., MOC) and 3–5 related docs with short summaries. Data is loaded from a static JSON produced by the knowledge-RAG pipeline (swap to real endpoint later).  
**Time**: ≤2h | **Risk**: Low (read-only, no infra changes).

---

### 1) File layout (additions only)

```
Costinel/
└── src/
    ├── components/
    │   └── TopHubSignalCard/
    │       ├── TopHubSignalCard.tsx
    │       ├── TopHubSignalCard.module.css
    ├── lib/
    │   └── topHubMock.ts
    ├── hooks/
    │   └── useTopHubSignal.ts
    └── pages/
        └── Dashboard/
            └── Dashboard.tsx   (add card import + placement)
```

---

### 2) Static data contract (`topHubMock.ts`)

```ts
// src/lib/topHubMock.ts
export interface RelatedDoc {
  id: string;
  title: string;
  summary: string;
  score: number;
  url?: string;
}

export interface TopHub {
  hubId: string;
  label: string;
  description: string;
  connections: number;
  lastUpdated: string;
  relatedDocs: RelatedDoc[];
}

export const topHubMock: TopHub = {
  hubId: "MOC",
  label: "MOC (Mission Operations Center)",
  description:
    "Most-connected hub across cost governance playbooks, runbooks, and policy artifacts.",
  connections: 42,
  lastUpdated: "2026-04-27T14:30:00Z",
  relatedDocs: [
    {
      id: "doc-001",
      title: "Cloud Cost Anomaly Playbook",
      summary: "Detect, triage, and signal cloud cost anomalies with owner assignment.",
      score: 0.92,
    },
    {
      id: "doc-042",
      title: "Reserved Instance Coverage Guide",
      summary: "Step-by-step RI recommendations and coverage analysis workflows.",
      score: 0.87,
    },
    {
      id: "doc-117",
      title: "Multi-Cloud Tagging Standard",
      summary: "Standardized tagging schema to unify AWS/GCP/Azure cost allocation and reporting.",
      score: 0.81,
    },
  ],
};
```

---

### 3) Hook: `useTopHubSignal.ts`

```ts
// src/hooks/useTopHubSignal.ts
import { useEffect, useState } from "react";
import { TopHub } from "../lib/topHubMock";

export function useTopHubSignal(jsonPath?: string) {
  const [signal, setSignal] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    const load = () => {
      // If a JSON path is provided, fetch it; otherwise use mock.
      if (jsonPath) {
        fetch(jsonPath)
          .then((res) => {
            if (!res.ok) throw new Error(`Failed to load top-hub signal: ${res.status}`);
            return res.json();
          })
          .then((data) => {
            if (mounted) {
              setSignal(data);
              setError(null);
            }
          })
          .catch((err) => {
            if (mounted) setError(err);
          })
          .finally(() => {
            if (mounted) setLoading(false);
          });
      } else {
        // Use mock synchronously for dev/preview.
        if (mounted) {
          // In real usage, import topHubMock or fetch from /data/topHubSignal.json
          import("../lib/topHubMock").then((mod) => {
            setSignal(mod.topHubMock);
            setError(null);
            setLoading(false);
          }).catch((err) => {
            if (mounted) setError(err);
            setLoading(false);
          });
        }
      }
    };

    load();

    return () => {
      mounted = false;
    };
  }, [jsonPath]);

  return { signal, loading, error };
}
```

---

### 4) Component: `TopHubSignalCard.tsx`

```tsx
// src/components/TopHubSignalCard/TopHubSignalCard.tsx
import React from "react";
import { useTopHubSignal } from "../../hooks/useTopHubSignal";
import styles from "./TopHubSignalCard.module.css";

export const TopHubSignalCard: React.FC<{ jsonPath?: string }> = ({ jsonPath }) => {
  const { signal, loading, error } = useTopHubSignal(jsonPath);

  if (loading) {
    return (
      <div className={styles.card}>
        <div className={styles.loading}>Loading top-hub insights…</div>
      </div>
    );
  }

  if (error || !signal) {
    return (
      <div className={styles.card}>
        <div className={styles.error}>Unable to load top-hub insights.</div>
      </div>
    );
  }

  return (
    <div className={styles.card}>
      <header className={styles.header}>
        <h3 className={styles.title}>Top Hub</h3>
        <span className={styles.badge}>{signal.label.split(" ")[0]}</span>
      </header>

      <p className={styles.hubDescription}>{signal.description}</p>
      <div className={styles.meta}>
        <span>{signal.connections} connections</span>
        <span>Updated {new Date(signal.lastUpdated).toLocaleDateString()}</span>
      </div>

      <section className={styles.related}>
        <h4 className={styles.relatedTitle}>Related docs</h4>
        <ul className={styles.list}>
          {signal.relatedDocs.map((doc) => (
            <li key={doc.id} className={styles.item}>
              <a
                href={doc.url || "#"}
                className={styles.link}
                target="_blank"
                rel="noopener noreferrer"
              >
                <div className={styles.itemTitle}>{doc.title}</div>
                <div className={styles.itemSummary}>{doc.summary}</div>
              </a>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
};
```

---

### 5) Styles: `TopHubSignalCard.module.css`

```css
/* src/components/TopHubSignalCard/TopHubSignalCard.module.css */
.card {
  background: #fff;
  border: 1px solid #e6e9ef;
  border-radius: 10px;
  padding: 18px 20px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}

.loading,
.error {
  color: #6b7280;
  font-size: 14px;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  color: #0f172a;
}

.badge {
  display: inline-block;
  background: #dbeafe;
  color: #1e40af;
  font-size: 12px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.hubDescription {
  margin: 6px 0 
