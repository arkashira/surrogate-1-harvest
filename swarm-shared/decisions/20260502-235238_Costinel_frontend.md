# Costinel / frontend

**Final merged implementation — strongest, correct, and actionable**

- **Scope**: Read-only frontend card (≤2h).  
- **Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution).  
- **Goal**: Surface the highest-scored hub (e.g., “MOC”) with score, concise insight, and quick navigation to related context — mock-first, real-API-ready, zero backend changes.

---

### Why this is highest value
- Directly implements the **top-hub doc insight** pattern (knowledge-RAG / graph).  
- Minimal surface (static-ish card) → low risk, fast ship.  
- Improves governance decision context without violating “Sense + Signal”.  
- Reusable component with a clear contract for future real API integration.

---

### Implementation steps (frontend only)

1. **Add types**  
   `src/types/knowledge-rag.ts` — canonical contract matching expected `/api/knowledge-rag/top-hub` shape.

2. **Create card component**  
   `src/components/TopHubSignalCard.tsx` — accepts `hub` prop, renders label, score badge, short summary (≤2 sentences), up to 3 related links, and “Review in Knowledge Graph” CTA (opens new tab). **No action buttons that mutate state.**

3. **Add mock service**  
   `src/services/mockKnowledgeRag.ts` — exports `getTopHub(): TopHub` with realistic mock (e.g., “MOC”). Designed to be swapped later with a real fetch to `/api/knowledge-rag/top-hub`.

4. **Compose into dashboard**  
   Import `TopHubSignalCard` into `src/pages/Dashboard.tsx` (or equivalent) and place near cost summary or governance section (top-right or under “Insights”). Ensure mobile responsive.

5. **Add tests (optional but recommended)**  
   Snapshot/render test for card. No e2e required for this read-only card.

6. **Verify & ship**  
   Run `npm run build` and `npm run lint`. Confirm no console errors and card renders with mock data. Commit and tag as `frontend/top-hub-signal-card`.

---

### Code artifacts

#### `src/types/knowledge-rag.ts`
```ts
export interface RelatedLink {
  label: string;
  href: string;
  external?: boolean;
}

export interface TopHub {
  hubId: string;
  name: string;
  score: number; // 0-100
  insight: string;
  relatedLinks: RelatedLink[];
}
```

#### `src/services/mockKnowledgeRag.ts`
```ts
import { TopHub } from "../types/knowledge-rag";

export const getTopHub = (): TopHub => ({
  hubId: "moc",
  name: "MOC",
  score: 92,
  insight:
    "Most-connected operational hub with high cross-service dependencies. Review reserved capacity and anomaly signals before scheduling changes.",
  relatedLinks: [
    { label: "Service map", href: "/services/moc/map" },
    { label: "Cost anomalies", href: "/anomalies?hub=moc" },
    { label: "Governance proposals", href: "/proposals?hub=moc" },
  ],
});
```

#### `src/components/TopHubSignalCard.tsx`
```tsx
import React from "react";
import "./TopHubSignalCard.css";
import { TopHub, RelatedLink } from "../types/knowledge-rag";

export interface TopHubSignalCardProps {
  hub: TopHub;
  graphUrl?: string; // optional link to full graph view
}

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  hub,
  graphUrl = `/knowledge-graph?hub=${encodeURIComponent(hub.hubId)}`,
}) => {
  const safeLinks = hub.relatedLinks?.slice(0, 3) ?? [];

  return (
    <div
      className="top-hub-signal-card"
      role="region"
      aria-label={`Top hub: ${hub.name}`}
    >
      <div className="card-header">
        <div className="hub-title">
          <span className="hub-label">{hub.name}</span>
          <span className="hub-score" title="Connection strength">
            {hub.score}
          </span>
        </div>
        <a
          className="graph-link"
          href={graphUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          Review in Knowledge Graph →
        </a>
      </div>

      <p className="hub-summary">{hub.insight}</p>

      {safeLinks.length > 0 && (
        <ul className="related-links" aria-label="Related links">
          {safeLinks.map((link: RelatedLink, idx: number) => (
            <li key={idx}>
              <a
                href={link.href}
                target={link.external ? "_blank" : "_self"}
                rel={link.external ? "noopener noreferrer" : undefined}
              >
                {link.label}
                {link.external && " ↗"}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
```

#### `src/components/TopHubSignalCard.css`
```css
.top-hub-signal-card {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  max-width: 420px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 8px;
}

.hub-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.hub-label {
  font-weight: 700;
  font-size: 16px;
  color: #0f172a;
}

.hub-score {
  font-size: 13px;
  font-weight: 600;
  color: #0ea5e9;
  background: #e0f2fe;
  padding: 2px 8px;
  border-radius: 99px;
}

.graph-link {
  font-size: 12px;
  color: #64748b;
  text-decoration: none;
  white-space: nowrap;
}

.graph-link:hover {
  color: #0f172a;
  text-decoration: underline;
}

.hub-summary {
  margin: 0 0 12px 0;
  font-size: 13px;
  color: #334155;
  line-height: 1.5;
}

.related-links {
  list-style: none;
  padding: 0;
  margin: 0;
  font-size: 13px;
}

.related-links li {
  margin: 4px 0;
}

.related-links a {
  color: #0f172a;
  text-decoration: none;
}

.related-links a:hover {
  text-decoration: underline;
}
```

#### Integrate into dashboard (`src/pages/Dashboard.tsx` — example snippet)
```tsx
import { TopHubSignalCard } from "../components/TopHubSignalCard";
import { getTopHub } from "../services/mockKnowledgeRag";

export const Dashboard = () => {
  return (
    <div className="dashboard">
      {/* existing content ... */}
      <aside className="insights-panel">
        <TopHubSignalCard hub={getTopHub()} />
      </aside>
    </div>
  );
};
```

---

### Acceptance checklist
- [x] Card is read-only (no POST/PUT/DELETE handlers).  
- [x] Uses mock service (swap-ready for real Knowledge-RAG API later).  
- [x] Renders hub name, score, insight, and ≤3 related links.  
- [x] Includes “Review in Knowledge Graph” link (opens new tab).  
- [x] Responsive and accessible (AR
