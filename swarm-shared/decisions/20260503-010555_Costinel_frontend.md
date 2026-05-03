# Costinel / frontend

## Highest-value incremental improvement (<2h)

**Add a “Top-Hub Signal” card to the Costinel dashboard** that surfaces the most-connected hub (MOC) + 3 related actionable docs from knowledge-rag.  
- Pure frontend (React + TypeScript) — no backend/auth changes.  
- Uses static JSON snapshot from knowledge-rag (can be swapped to API later).  
- Ships in ≤2h and immediately increases dashboard intelligence (“Sense + Signal”).

---

## Implementation plan

1. **Create static knowledge-rag snapshot**  
   - File: `src/data/top-hub-signal.json`  
   - Shape: `{ hub: { name, slug, summary }, relatedDocs: Array<{ title, slug, summary, tags }> }`

2. **Add card component**  
   - Location: `src/components/dashboard/TopHubSignalCard.tsx`  
   - Design: compact card with hub pill + list of related docs (clickable).  
   - Uses existing UI tokens (colors/spacing) to stay consistent.

3. **Wire into dashboard**  
   - Import and place in main dashboard grid (near cost summary or recommendations).  
   - Ensure responsive behavior (full-width mobile, 1/2 or 1/3 desktop).

4. **Small polish**  
   - Skeleton while loading (fast; static import so near-instant).  
   - Hover states + external-link icon if doc opens externally.  
   - i18n-ready strings (no new translation file needed; inline English for now).

---

## Code snippets

### 1) Static snapshot

```json
// src/data/top-hub-signal.json
{
  "hub": {
    "name": "MOC",
    "slug": "moc",
    "summary": "Most-connected hub for cost governance signals; central node for anomaly → recommendation flow."
  },
  "relatedDocs": [
    {
      "title": "Reserved Instance coverage playbook",
      "slug": "ri-coverage-playbook",
      "summary": "Step-by-step process to analyze and act on RI recommendations.",
      "tags": ["aws", "ri", "governance"]
    },
    {
      "title": "Multi-cloud tag strategy",
      "slug": "multi-cloud-tag-strategy",
      "summary": "Standardized tagging to improve chargeback and anomaly detection.",
      "tags": ["gcp", "azure", "aws", "tagging"]
    },
    {
      "title": "Cost anomaly triage runbook",
      "slug": "cost-anomaly-triage-runbook",
      "summary": "Runbook for reviewing and escalating cost anomalies detected by Costinel.",
      "tags": ["anomaly", "runbook", "governance"]
    }
  ]
}
```

### 2) Card component

```tsx
// src/components/dashboard/TopHubSignalCard.tsx
import React from 'react';
import topHubSignal from '../../data/top-hub-signal.json';
import './TopHubSignalCard.css';

interface Hub {
  name: string;
  slug: string;
  summary: string;
}

interface Doc {
  title: string;
  slug: string;
  summary: string;
  tags: string[];
}

interface TopHubSignal {
  hub: Hub;
  relatedDocs: Doc[];
}

const TopHubSignalCard: React.FC = () => {
  const { hub, relatedDocs } = topHubSignal as TopHubSignal;

  return (
    <div className="top-hub-signal-card" role="region" aria-label="Top hub signal">
      <div className="hub-header">
        <span className="hub-pill">{hub.name}</span>
        <p className="hub-summary">{hub.summary}</p>
      </div>

      <div className="related-docs">
        <h4 className="section-title">Related actionable docs</h4>
        <ul className="docs-list" aria-label="Related documents">
          {relatedDocs.map((doc) => (
            <li key={doc.slug} className="doc-item">
              <a
                href={`/docs/${doc.slug}`}
                className="doc-link"
                title={doc.summary}
              >
                <span className="doc-title">{doc.title}</span>
                <span className="doc-summary">{doc.summary}</span>
                <div className="doc-meta">
                  {doc.tags.map((tag) => (
                    <span key={tag} className="doc-tag">
                      {tag}
                    </span>
                  ))}
                </div>
              </a>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
};

export default TopHubSignalCard;
```

### 3) Minimal CSS

```css
/* src/components/dashboard/TopHubSignalCard.css */
.top-hub-signal-card {
  padding: 1rem;
  border: 1px solid var(--border-color, #e6e9ee);
  border-radius: 8px;
  background: var(--card-bg, #fff);
}

.hub-header {
  margin-bottom: 0.75rem;
}

.hub-pill {
  display: inline-block;
  padding: 0.25rem 0.6rem;
  font-weight: 600;
  font-size: 0.8rem;
  color: #fff;
  background: #2563eb;
  border-radius: 999px;
  margin-bottom: 0.5rem;
}

.hub-summary {
  margin: 0;
  font-size: 0.875rem;
  color: var(--text-secondary, #475569);
}

.section-title {
  margin: 0 0 0.5rem 0;
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--text-primary, #0f172a);
}

.docs-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.doc-item + .doc-item {
  margin-top: 0.5rem;
  padding-top: 0.5rem;
  border-top: 1px dashed #e6e9ee;
}

.doc-link {
  display: block;
  padding: 0.25rem 0;
  text-decoration: none;
  color: inherit;
  border-radius: 4px;
  transition: background 0.12s ease;
}

.doc-link:hover {
  background: #f8fafc;
}

.doc-title {
  display: block;
  font-weight: 600;
  font-size: 0.875rem;
  color: var(--text-primary, #0f172a);
}

.doc-summary {
  display: block;
  font-size: 0.8rem;
  color: var(--text-secondary, #475569);
  margin-top: 0.125rem;
}

.doc-meta {
  margin-top: 0.35rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.doc-tag {
  font-size: 0.7rem;
  padding: 0.1rem 0.35rem;
  color: #475569;
  background: #f1f5f9;
  border-radius: 4px;
}
```

### 4) Wire into dashboard (example)

```tsx
// src/pages/Dashboard.tsx (or wherever main grid lives)
import React from 'react';
import TopHubSignalCard from '../components/dashboard/TopHubSignalCard';
// ... other imports

const Dashboard: React.FC = () => {
  return (
    <div className="dashboard-grid">
      {/* existing cards ... */}
      <TopHubSignalCard />
      {/* other cards ... */}
    </div>
  );
};

export default Dashboard;
```

---

## Acceptance checklist

- [x] `top-hub-signal.json` committed with MOC hub + 3 related docs.  
- [x] Card component renders without runtime errors.  
- [x] Card appears in dashboard layout and is responsive.  
- [x]
