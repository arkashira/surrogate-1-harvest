# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default **MOC**) and its actionable proposals from the knowledge graph.  
- Resilient to missing backend: layered fallback — live API → CDN snapshot → local static payload.  
- Ships in **<2h**: single component + one service util + small layout change; no schema changes, no migrations.

---

### Files to modify/create
- `src/lib/api/knowledgeGraph.ts` (new service util)  
- `src/components/TopHubSignalPanel/TopHubSignalPanel.tsx` (new)  
- `src/components/TopHubSignalPanel/TopHubSignalPanel.module.css` (new)  
- `src/assets/data/top-hub-fallback.json` (static fallback)  
- Dashboard layout file (insert panel)  
- `tests/unit/TopHubSignalPanel.spec.tsx` (new)

---

### 1) Knowledge graph service (layered fetch)

```ts
// src/lib/api/knowledgeGraph.ts
import fallback from '@/assets/data/top-hub-fallback.json';

const HUB_API = '/api/knowledge-graph/top-hub';
const CDN_HUB = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json';

export interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  actionUrl?: string;
  tags?: string[];
}

export interface TopHubPayload {
  hub: {
    id: string;
    label: string;
    description?: string;
    connectionsCount: number;
  };
  proposals: Proposal[];
  generatedAt: string;
}

export async function fetchTopHubSignal(hub = 'MOC'): Promise<TopHubPayload> {
  // 1) Backend (preferred)
  try {
    const res = await fetch(`${HUB_API}?hub=${encodeURIComponent(hub)}`, {
      credentials: 'same-origin',
      cache: 'no-cache'
    });
    if (res.ok) return res.json();
  } catch {
    // noop
  }

  // 2) CDN snapshot
  try {
    const res = await fetch(CDN_HUB, { cache: 'no-store' });
    if (res.ok) return res.json();
  } catch {
    // noop
  }

  // 3) Local static fallback (always available)
  return fallback as TopHubPayload;
}
```

---

### 2) Component

```tsx
// src/components/TopHubSignalPanel/TopHubSignalPanel.tsx
import React, { useEffect, useState } from 'react';
import { fetchTopHubSignal, type TopHubPayload, type Proposal } from '../../lib/api/knowledgeGraph';
import styles from './TopHubSignalPanel.module.css';

function ProposalItem({ proposal }: { proposal: Proposal }) {
  return (
    <article className={styles.proposal} role="listitem">
      <div className={styles.proposalHeader}>
        <h4 className={styles.proposalTitle}>{proposal.title}</h4>
        <span className={styles[`impact_${proposal.impact}`]}>{proposal.impact}</span>
      </div>
      <p className={styles.proposalSummary}>{proposal.summary}</p>
      {proposal.tags && proposal.tags.length > 0 && (
        <ul className={styles.tags} aria-label="Tags">
          {proposal.tags.map((t) => (
            <li key={t} className={styles.tag}>
              {t}
            </li>
          ))}
        </ul>
      )}
      {proposal.actionUrl && (
        <a
          className={styles.actionLink}
          href={proposal.actionUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          View details
        </a>
      )}
    </article>
  );
}

export default function TopHubSignalPanel() {
  const [payload, setPayload] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      const data = await fetchTopHubSignal();
      if (mounted) {
        setPayload(data);
        setLoading(false);
      }
    }
    load();
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <section className={styles.panel} aria-busy="true">
        <div className={styles.loading}>Loading top-hub signals…</div>
      </section>
    );
  }

  if (!payload) {
    return (
      <section className={styles.panel} aria-label="Top hub signals unavailable">
        <div className={styles.empty}>Top-hub signals unavailable at this time.</div>
      </section>
    );
  }

  return (
    <section className={styles.panel} aria-label={`Top hub: ${payload.hub.label}`}>
      <header className={styles.header}>
        <h3 className={styles.hubLabel}>{payload.hub.label}</h3>
        <p className={styles.hubMeta}>
          {payload.hub.connectionsCount} connections • updated {new Date(payload.generatedAt).toLocaleDateString()}
        </p>
        {payload.hub.description && <p className={styles.hubDescription}>{payload.hub.description}</p>}
      </header>

      <div className={styles.proposals} role="list">
        {payload.proposals.length === 0 ? (
          <div className={styles.empty}>No actionable proposals at this time.</div>
        ) : (
          payload.proposals.map((p) => <ProposalItem key={p.id} proposal={p} />)
        )}
      </div>
    </section>
  );
}
```

---

### 3) Styles (module.css)

```css
/* src/components/TopHubSignalPanel/TopHubSignalPanel.module.css */
.panel {
  border: 1px solid var(--border, #e6e9ee);
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(16,24,40,0.04);
}

.header {
  margin-bottom: 12px;
}

.hubLabel {
  margin: 0 0 4px 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary, #0f172a);
}

.hubMeta {
  margin: 0 0 6px 0;
  font-size: 13px;
  color: var(--text-muted, #64748b);
}

.hubDescription {
  margin: 0;
  font-size: 14px;
  color: var(--text-muted, #64748b);
}

.proposals {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.proposal {
  padding: 12px;
  border-radius: 6px;
  background: var(--bg-subtle, #f8fafc);
  border: 1px solid var(--border-subtle, #eef2f7);
}

.proposalHeader {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.proposalTitle {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary, #0f172a);
}

.proposalSummary {
  margin: 0 0 8px 0;
  font-size: 13px;
  color: var(--text-muted, #475569);
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0 0 
