# Costinel / backend

## Final Implementation Plan — Top-hub Signal Panel (Costinel Dashboard)

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and actionable proposals from the knowledge graph.  
**Timebox**: <2h  
**Risk**: None (no backend changes; resilient to missing endpoint).

---

### 1) Files to modify / create
- `src/pages/Dashboard/Dashboard.tsx` — add `<TopHubSignalPanel />` near the top of the content area.
- `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx` — new component.
- `src/components/TopHubSignalPanel/TopHubSignalPanel.css` — minimal styles.
- `src/types/knowledge.ts` — add types for hub/proposal payload.

---

### 2) Types (`src/types/knowledge.ts`)
```ts
export interface KnowledgeHub {
  slug: string;       // e.g. "MOC"
  label: string;      // e.g. "Mission Operations Center"
  rank: number;       // connection strength
  description?: string;
}

export interface KnowledgeProposal {
  id: string;
  title: string;
  summary: string;
  action: string;     // human action required
  impact: 'high' | 'medium' | 'low';
  href?: string;      // link to details or change mgmt
}

export interface TopHubPayload {
  hub: KnowledgeHub;
  proposals: KnowledgeProposal[];
  generatedAt: string; // ISO
}
```

---

### 3) Component (`src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`)
```tsx
import React, { useEffect, useState } from 'react';
import { KnowledgeHub, KnowledgeProposal, TopHubPayload } from '../../types/knowledge';
import './TopHubSignalPanel.css';

const FALLBACK: TopHubPayload | null = null;

const TopHubSignalPanel: React.FC = () => {
  const [payload, setPayload] = useState<TopHubPayload | null>(FALLBACK);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    fetch('/api/knowledge/top-hub', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: TopHubPayload) => {
        if (mounted) {
          setPayload(data);
          setError(false);
        }
      })
      .catch(() => {
        if (mounted) {
          setPayload(FALLBACK);
          setError(true);
        }
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  // Graceful silent fail: do not block dashboard
  if (error || (!loading && !payload)) return null;
  if (loading) {
    return (
      <div className="top-hub-panel loading" role="status" aria-label="Loading top hub signal">
        <div className="skeleton" />
      </div>
    );
  }

  const { hub, proposals } = payload!;

  return (
    <div className="top-hub-panel" role="region" aria-label={`Top hub: ${hub.label}`}>
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <h3 className="top-hub-title">{hub.label}</h3>
        {hub.description && <p className="top-hub-desc">{hub.description}</p>}
      </div>

      <div className="top-hub-proposals">
        {proposals.length === 0 ? (
          <p className="no-proposals">No actionable proposals at this time.</p>
        ) : (
          proposals.map((p) => (
            <a
              key={p.id}
              className={`proposal-card impact-${p.impact}`}
              href={p.href || '#'}
              target="_blank"
              rel="noopener noreferrer"
            >
              <div className="proposal-title">{p.title}</div>
              <div className="proposal-summary">{p.summary}</div>
              <div className="proposal-meta">
                <span className={`impact-badge impact-${p.impact}`}>{p.impact}</span>
                <span className="proposal-action">{p.action}</span>
              </div>
            </a>
          ))
        )}
      </div>
    </div>
  );
};

export default TopHubSignalPanel;
```

---

### 4) Styles (`src/components/TopHubSignalPanel/TopHubSignalPanel.css`)
```css
.top-hub-panel {
  border: 1px solid var(--border-color, #e6e9ef);
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  margin-bottom: 16px;
}

.top-hub-panel.loading .skeleton {
  height: 80px;
  background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: loading 1.2s infinite;
  border-radius: 4px;
}

@keyframes loading {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.top-hub-header {
  margin-bottom: 12px;
}

.top-hub-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #2563eb;
  background: #eff6ff;
  padding: 2px 8px;
  border-radius: 4px;
  margin-bottom: 6px;
}

.top-hub-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary, #111827);
}

.top-hub-desc {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--text-secondary, #6b7280);
}

.top-hub-proposals {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.proposal-card {
  display: block;
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid #f1f5f9;
  background: #fafbfc;
  text-decoration: none;
  color: inherit;
  transition: box-shadow 0.12s, border-color 0.12s;
}

.proposal-card:hover {
  box-shadow: 0 1px 3px rgba(16,24,40,0.06);
  border-color: #cbd5e1;
}

.proposal-title {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary, #111827);
  margin-bottom: 4px;
}

.proposal-summary {
  font-size: 13px;
  color: var(--text-secondary, #6b7280);
  margin-bottom: 6px;
}

.proposal-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-tertiary, #9ca3af);
}

.impact-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;

