# Costinel / frontend

## Costinel — Top-Hub Signal Card (frontend-only, ≤2h)

### Scope & Constraints
- **Pure frontend** — no backend, no new APIs, no auth changes.
- **Read-only** — Sense + Signal only (no execute).
- **Timeboxed ≤2h** — minimal, high-value UI.
- **Graceful fallback** — works when knowledge-rag/graph data is missing.
- **Reuse existing patterns** — follow project conventions (icons, colors, layout).

---

### Implementation Plan (120 min)

| Time | Task |
|------|------|
| 0–15 min | Inspect project structure; identify where to add card (likely dashboard layout). Create `TopHubSignalCard` component. |
| 15–35 min | Design minimal UI: hub title, rank, connection count, short insight, related docs list, last updated. Use existing design tokens (colors, spacing). |
| 35–55 min | Add mock/local data shape + graceful fallback (empty state). Implement lightweight data fetch stub (no real API) that can later be swapped to real graph endpoint. |
| 55–80 min | Wire into dashboard page (or wherever top-level signals appear). Ensure responsive (mobile/desktop). |
| 80–100 min | Polish: loading states, empty states, copy, icons, accessibility (aria labels, keyboard nav). |
| 100–115 min | Add tests (optional) or at least manual smoke checks. |
| 115–120 min | Commit with clear message; verify build passes. |

---

### Component: `TopHubSignalCard.tsx`

```tsx
// src/components/TopHubSignalCard.tsx
import React from 'react';
import './TopHubSignalCard.css';

export interface RelatedDoc {
  title: string;
  slug: string;
  relevance: number; // 0-1
}

export interface TopHubSignal {
  hub: string;
  rank: number;
  connectionCount: number;
  insight: string;
  relatedDocs: RelatedDoc[];
  lastUpdated: string; // ISO
}

interface TopHubSignalCardProps {
  signal?: TopHubSignal;
  loading?: boolean;
}

const fallbackSignal: TopHubSignal = {
  hub: 'MOC',
  rank: 1,
  connectionCount: 42,
  insight:
    'Most-connected hub indicates central governance workflows. Prioritize signal routing and policy templates around this node.',
  relatedDocs: [
    { title: 'Governance Playbook', slug: 'governance-playbook', relevance: 0.92 },
    { title: 'RI Coverage Guide', slug: 'ri-coverage-guide', relevance: 0.81 },
  ],
  lastUpdated: new Date().toISOString(),
};

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  signal,
  loading = false,
}) => {
  const data = signal || fallbackSignal;

  if (loading) {
    return (
      <div className="top-hub-card loading" aria-busy="true">
        <div className="skeleton title" />
        <div className="skeleton row" />
        <div className="skeleton row short" />
      </div>
    );
  }

  return (
    <article className="top-hub-card" aria-label={`Top hub signal: ${data.hub}`}>
      <header className="top-hub-card__header">
        <div>
          <h3 className="top-hub-card__title">{data.hub}</h3>
          <p className="top-hub-card__sub">
            Rank #{data.rank} · {data.connectionCount} connections
          </p>
        </div>
        <time className="top-hub-card__time" dateTime={data.lastUpdated}>
          {new Date(data.lastUpdated).toLocaleDateString(undefined, {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
          })}
        </time>
      </header>

      <p className="top-hub-card__insight">{data.insight}</p>

      {data.relatedDocs.length > 0 && (
        <div className="top-hub-card__docs">
          <h4 className="top-hub-card__docs-title">Related docs</h4>
          <ul>
            {data.relatedDocs.map((doc) => (
              <li key={doc.slug}>
                <a
                  href={`/docs/${doc.slug}`}
                  className="top-hub-card__doc-link"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {doc.title}
                  <span className="top-hub-card__relevance">
                    {Math.round(doc.relevance * 100)}%
                  </span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
};
```

---

### Styles: `TopHubSignalCard.css`

```css
/* src/components/TopHubSignalCard.css */
.top-hub-card {
  background: #fff;
  border: 1px solid #e6e9ee;
  border-radius: 10px;
  padding: 16px;
  max-width: 420px;
  font-family: system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue',
    Arial;
  color: #1f2937;
}

.top-hub-card__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 8px;
}

.top-hub-card__title {
  font-size: 1.125rem;
  font-weight: 700;
  margin: 0;
  color: #0ea5e9;
}

.top-hub-card__sub {
  margin: 2px 0 0;
  font-size: 0.875rem;
  color: #6b7280;
}

.top-hub-card__time {
  font-size: 0.75rem;
  color: #9ca3af;
  white-space: nowrap;
}

.top-hub-card__insight {
  margin: 8px 0 12px;
  font-size: 0.9375rem;
  line-height: 1.5;
  color: #374151;
}

.top-hub-card__docs-title {
  font-size: 0.8125rem;
  font-weight: 600;
  margin: 0 0 6px;
  color: #4b5563;
  text-transform: uppercase;
  letter-spacing: 0.025em;
}

.top-hub-card__docs ul {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.top-hub-card__doc-link {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.875rem;
  color: #0ea5e9;
  text-decoration: none;
}

.top-hub-card__doc-link:hover {
  text-decoration: underline;
}

.top-hub-card__relevance {
  font-size: 0.75rem;
  color: #10b981;
  font-weight: 600;
}

/* Loading skeletons */
.top-hub-card.loading {
  pointer-events: none;
}

.top-hub-card .skeleton {
  background: #e6e9ee;
  border-radius: 4px;
  animation: pulse 1.2s ease-in-out infinite;
}

.top-hub-card .skeleton.title {
  height: 20px;
  width: 60%;
  margin-bottom: 6px;
}

.top-hub-card .skeleton.row {
  height: 12px;
  width: 90%;
  margin-bottom: 4px;
}

.top-hub-card .skeleton.row
