# Costinel / quality

## Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal / most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph. CDN-first data:

- Single API call from orchestration layer (Mac) to `list_repo_tree` once per day → saves `top-hub.json` to CDN (`/datasets/Costinel/top-hub/resolve/main/{date}/top-hub.json`).  
- Frontend fetches via CDN URL (no Authorization, bypasses HF API rate limits).  
- Panel shows: hub name, centrality score, top 5 actionable proposals (title, signal strength, due date, owner), and quick links to full context.

**Time budget**: ~90 min (frontend only).

---

### 1) File layout (additions)

```
Costinel/
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel/
│   │       ├── TopHubSignalPanel.tsx
│   │       ├── TopHubSignalPanel.module.css
│   │       └── index.ts
│   ├── hooks/
│   │   └── useCDNTopHub.ts
│   └── types/
│       └── knowledgeGraph.ts
└── public/
    └── config/
        └── cdn.json          # CDN base + repo path
```

---

### 2) Types (`src/types/knowledgeGraph.ts`)

```ts
export interface Proposal {
  id: string;
  title: string;
  signalStrength: number; // 0-100
  dueDate?: string;       // ISO
  owner?: string;
  contextUrl?: string;
  tags?: string[];
}

export interface TopHubPayload {
  hub: string;
  centrality: number;
  updatedAt: string;       // ISO
  proposals: Proposal[];
}
```

---

### 3) CDN hook (`src/hooks/useCDNTopHub.ts`)

```ts
import { useEffect, useState } from 'react';
import { TopHubPayload } from '../types/knowledgeGraph';

const CDN_BASE = (window as any).__CDN_BASE__ || 'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main';
const HUB_PATH = 'top-hub'; // e.g. top-hub/2026-05-03/top-hub.json

export function useCDNTopHub(dateFolder?: string) {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const today = dateFolder || new Date().toISOString().slice(0, 10);
    const url = `${CDN_BASE}/${HUB_PATH}/${today}/top-hub.json?cachebust=${Date.now()}`;

    fetch(url, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
        return res.json();
      })
      .then((json: TopHubPayload) => setData(json))
      .catch((err) => setError(err))
      .finally(() => setLoading(false));
  }, [dateFolder]);

  return { data, loading, error };
}
```

---

### 4) Panel component (`src/components/TopHubSignalPanel/TopHubSignalPanel.tsx`)

```tsx
import React from 'react';
import { TopHubPayload, Proposal } from '../../types/knowledgeGraph';
import { useCDNTopHub } from '../../hooks/useCDNTopHub';
import styles from './TopHubSignalPanel.module.css';

function ProposalRow({ p }: { p: Proposal }) {
  return (
    <div className={styles.proposalRow}>
      <div className={styles.signalBar} style={{ width: `${p.signalStrength}%` }} />
      <div className={styles.meta}>
        <div className={styles.title}>{p.title}</div>
        <div className={styles.sub}>
          <span>Signal {p.signalStrength}</span>
          {p.dueDate && <span>Due {new Date(p.dueDate).toLocaleDateString()}</span>}
          {p.owner && <span>Owner {p.owner}</span>}
        </div>
      </div>
      {p.contextUrl && (
        <a href={p.contextUrl} target="_blank" rel="noopener noreferrer" className={styles.link}>
          Context
        </a>
      )}
    </div>
  );
}

export default function TopHubSignalPanel({ dateFolder }: { dateFolder?: string }) {
  const { data, loading, error } = useCDNTopHub(dateFolder);

  if (loading) return <div className={styles.panel}>Loading top hub signal…</div>;
  if (error || !data) return <div className={styles.panel}>Unable to load top hub signal.</div>;

  const topProposals: Proposal[] = data.proposals.slice(0, 5);

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <div>
          <span className={styles.hub}>🧠 {data.hub}</span>
          <span className={styles.centrality}>Centrality {(data.centrality * 100).toFixed(1)}</span>
        </div>
        <div className={styles.updated}>Updated {new Date(data.updatedAt).toLocaleString()}</div>
      </div>

      <div className={styles.proposals}>
        {topProposals.length === 0 ? (
          <div className={styles.empty}>No actionable proposals for this hub.</div>
        ) : (
          topProposals.map((p) => <ProposalRow key={p.id} p={p} />)
        )}
      </div>

      <div className={styles.footer}>
        <a href={`/knowledge-graph?hub=${encodeURIComponent(data.hub)}`} className={styles.more}>
          View full hub context →
        </a>
      </div>
    </div>
  );
}
```

---

### 5) Styles (`src/components/TopHubSignalPanel/TopHubSignalPanel.module.css`)

```css
.panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 16px;
  background: #fff;
  font-family: Inter, system-ui, sans-serif;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 12px;
  gap: 12px;
}

.hub {
  font-weight: 700;
  font-size: 16px;
  color: #0f172a;
  margin-right: 12px;
}

.centrality {
  font-size: 13px;
  color: #64748b;
}

.updated {
  font-size: 12px;
  color: #94a3b8;
  text-align: right;
}

.proposals {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.proposalRow {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px dashed #f1f5f9;
}

.signalBar {
  height: 6px;
  min-width: 40px;
  background: #10b981;
  border-radius: 3px;
  flex-shrink: 0;
}

.meta {
  flex: 1;
  min-width: 0;
}

.title {
  font-size: 14px;
  font-weight: 600;
  color: #0f172a;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sub {
  font-size: 12px;

