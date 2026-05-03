# Costinel / backend

## Implementation Plan — Costinel “Top-Hub Signal” Card (frontend-only, ≤2h)

### Chosen approach
- Pure frontend React + TypeScript, no backend/API/auth changes.
- **Static top-hub + related docs** wired from knowledge-rag graph (MOC = top hub).
- Renders a dashboard card with hub summary + related doc links + “Last updated” timestamp.
- Uses existing design tokens and responsive layout.

### Files to create/modify
- `src/components/CostinelTopHubSignalCard.tsx` — new card component
- `src/pages/Dashboard.tsx` — mount card into existing dashboard grid
- `src/types/knowledgeRag.ts` — types for hub + related doc
- `src/data/topHubMock.ts` — static data (MOC hub + related docs)

---

### 1) Types (`src/types/knowledgeRag.ts`)

```ts
export interface RelatedDoc {
  slug: string;
  title: string;
  summary: string;
  url: string;
  tags: string[];
}

export interface TopHub {
  hubId: string;
  title: string;
  summary: short string;
  lastUpdated: string; // ISO
  relatedDocs: RelatedDoc[];
}
```

---

### 2) Static data (`src/data/topHubMock.ts`)

```ts
import { TopHub } from '../types/knowledgeRag';

export const topHubMock: TopHub = {
  hubId: 'MOC',
  title: 'Mission Operations Center',
  summary:
    'Central hub for mission-critical runbooks, on-call rotations, and incident response playbooks. Highest connectivity across cost governance and operational runbooks.',
  lastUpdated: '2026-04-27T14:30:00Z',
  relatedDocs: [
    {
      slug: 'cost-governance-runbook',
      title: 'Cost Governance Runbook',
      summary: 'Step-by-step runbook for cost anomaly triage and approval workflows.',
      url: '/docs/cost-governance-runbook',
      tags: ['runbook', 'cost', 'governance'],
    },
    {
      slug: 'oncall-handoff',
      title: 'On-Call Handoff Guide',
      summary: 'Standardized handoff checklist and escalation paths for MOC shifts.',
      url: '/docs/oncall-handoff',
      tags: ['oncall', 'moc', 'process'],
    },
    {
      slug: 'incident-response-playbook',
      title: 'Incident Response Playbook',
      summary: 'Playbook for severity-based response, comms, and postmortems.',
      url: '/docs/incident-response-playbook',
      tags: ['incident', 'response', 'playbook'],
    },
  ],
};
```

---

### 3) Card component (`src/components/CostinelTopHubSignalCard.tsx`)

```tsx
import React from 'react';
import { TopHub } from '../types/knowledgeRag';
import { topHubMock } from '../data/topHubMock';
import './CostinelTopHubSignalCard.css';

interface Props {
  hub?: TopHub;
}

export const CostinelTopHubSignalCard: React.FC<Props> = ({ hub = topHubMock }) => {
  return (
    <div className="top-hub-card" role="region" aria-label={`Top hub: ${hub.title}`}>
      <div className="top-hub-header">
        <div>
          <span className="top-hub-badge">TOP HUB</span>
          <h3 className="top-hub-title">{hub.title}</h3>
          <p className="top-hub-summary">{hub.summary}</p>
        </div>
        <time className="top-hub-updated" dateTime={hub.lastUpdated}>
          Updated {new Date(hub.lastUpdated).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
        </time>
      </div>

      <ul className="related-docs-list" aria-label="Related documents">
        {hub.relatedDocs.map((doc) => (
          <li key={doc.slug} className="related-doc-item">
            <a href={doc.url} className="related-doc-link" target="_blank" rel="noopener noreferrer">
              <span className="related-doc-title">{doc.title}</span>
              <p className="related-doc-summary">{doc.summary}</p>
              <div className="related-doc-meta">
                {doc.tags.map((t) => (
                  <span key={t} className="related-doc-tag">
                    {t}
                  </span>
                ))}
              </div>
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
};
```

---

### 4) Styles (`src/components/CostinelTopHubSignalCard.css`)

```css
.top-hub-card {
  background: #fff;
  border: 1px solid #e6e9ef;
  border-radius: 10px;
  padding: 18px 20px;
  box-shadow: 0 1px 2px rgba(16,24,40,0.04);
}

.top-hub-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

.top-hub-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  color: #2563eb;
  background: #eff6ff;
  padding: 4px 8px;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 6px;
}

.top-hub-title {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
  margin: 0 0 6px 0;
}

.top-hub-summary {
  font-size: 13px;
  color: #475569;
  margin: 0;
  line-height: 1.4;
}

.top-hub-updated {
  font-size: 12px;
  color: #64748b;
  white-space: nowrap;
}

.related-docs-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.related-doc-item {
  border-radius: 6px;
  overflow: hidden;
}

.related-doc-link {
  display: block;
  padding: 10px 12px;
  border: 1px solid #f1f5f9;
  border-radius: 6px;
  text-decoration: none;
  color: inherit;
  transition: border-color 0.15s, background 0.15s;
}

.related-doc-link:hover {
  border-color: #cbd5e1;
  background: #fafbfc;
}

.related-doc-title {
  display: block;
  font-weight: 600;
  font-size: 13px;
  color: #0f172a;
  margin-bottom: 4px;
}

.related-doc-summary {
  font-size: 12px;
  color: #475569;
  margin: 0 0 6px 0;
  line-height: 1.4;
}

.related-doc-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.related-doc-tag {
  font-size: 11px;
  color: #64748b;
  background: #f8fafc;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid #eef2f7;
}
```

---

### 5) Mount into Dashboard (`src/pages/Dashboard.tsx`)

Locate the dashboard grid and add the card near the top
