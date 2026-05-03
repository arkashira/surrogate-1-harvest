# Costinel / backend

## Final Implementation — Costinel “Top-Hub Signal” Card  
*(frontend-only, read-only, ≤2h, production-ready)*

### Chosen approach
- Pure frontend React + TypeScript, no backend/API/auth changes.
- **Static top-hub + related docs** (checked-in JSON) for speed and reliability.
- Optional async loader stub so teams can swap in real knowledge-rag/graph queries later without changing the component API.
- Graceful empty/error states and compact mode.
- Reuse existing design tokens and patterns.

---

### File changes
1. `src/data/topHubSignal.json` — static hub + insights + docs.  
2. `src/components/cards/TopHubSignalCard.tsx` — new card component.  
3. `src/components/cards/index.ts` — export addition.  
4. Mount in dashboard (e.g., `src/pages/CostDashboard.tsx` or `App.tsx`).

---

### 1) Static data
`src/data/topHubSignal.json`
```json
{
  "topHub": "MOC",
  "hubId": "hub-moc",
  "connectionCount": 42,
  "description": "Multi-org cost governance hub — central recommendations and policy signals.",
  "lastUpdated": "2026-05-03T04:00:00Z",
  "topDocs": [
    {
      "title": "Reserved Instance coverage analysis",
      "url": "/docs/ri-coverage",
      "snippet": "Across 12 accounts, 68% RI coverage; opportunity to increase to 80% with 12-month commitments.",
      "score": 0.92,
      "updatedAt": "2026-05-02T08:14:00Z"
    },
    {
      "title": "Anomaly detection runbook",
      "url": "/docs/anomalies",
      "snippet": "Playbook for reacting to cost spikes and tag-drift signals.",
      "score": 0.87,
      "updatedAt": "2026-05-01T18:00:00Z"
    },
    {
      "title": "Cloud governance policy catalog",
      "url": "/docs/policies",
      "snippet": "Active policy set for production accounts (guardrails, not execution).",
      "score": 0.81,
      "updatedAt": "2026-04-30T12:00:00Z"
    }
  ]
}
```

---

### 2) Component
`src/components/cards/TopHubSignalCard.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { ExternalLink, AlertCircle, TrendingUp } from 'lucide-react';
import './TopHubSignalCard.css';

export interface RelatedDoc {
  title: string;
  url?: string;
  snippet: string;
  score?: number;
  updatedAt?: string;
}

export interface HubInsight {
  topHub: string;
  hubId?: string;
  connectionCount: number;
  description?: string;
  topDocs: RelatedDoc[];
  lastUpdated?: string;
}

interface TopHubSignalCardProps {
  /** Preloaded insight (optional). If provided, component will not auto-load. */
  insight?: HubInsight | null;
  /** Optional loader to fetch HubInsight (for future real graph/rag queries). */
  loader?: () => Promise<HubInsight | null>;
  /** Compact UI (fewer docs, tighter spacing) */
  compact?: boolean;
}

const defaultLoader = async (): Promise<HubInsight | null> => {
  try {
    const res = await import('../../data/topHubSignal.json');
    return res.default || res;
  } catch {
    return null;
  }
};

const timeAgo = (iso?: string): string => {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const hours = Math.floor(diff / 3600000);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
};

const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  insight: propInsight,
  loader = defaultLoader,
  compact = false,
}) => {
  const [insight, setInsight] = useState<HubInsight | null | undefined>(propInsight);
  const [loading, setLoading] = useState<boolean>(!propInsight);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (propInsight !== undefined) {
      setInsight(propInsight);
      setLoading(false);
      return;
    }

    let mounted = true;
    setLoading(true);
    loader()
      .then((result) => {
        if (mounted) {
          setInsight(result);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (mounted) {
          setError(err?.message || 'Failed to load hub insight');
          setInsight(null);
          setLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, [propInsight, loader]);

  if (loading) {
    return (
      <div className="thsc-card thsc-card-loading" role="status" aria-label="Loading top hub signal">
        <div className="thsc-loader-placeholder">
          <div className="thsc-row"><div className="thsc-bar short" /></div>
          <div className="thsc-row"><div className="thsc-bar long" /></div>
          <div className="thsc-row"><div className="thsc-bar medium" /></div>
        </div>
      </div>
    );
  }

  if (error || !insight) {
    return (
      <div className="thsc-card thsc-card-empty" role="status" aria-label="Top hub unavailable">
        <div className="thsc-empty-icon">
          <AlertCircle size={28} />
        </div>
        <p className="thsc-empty-title">Top hub unavailable</p>
        <p className="thsc-empty-desc">
          Knowledge-rag/graph insights are offline. Use manual review or try again later.
        </p>
        <small className="thsc-empty-note">Sense + Signal — No execution</small>
      </div>
    );
  }

  const docsToShow = insight.topDocs?.slice(0, compact ? 2 : 4) || [];

  return (
    <article className={`thsc-card thsc-top-hub-card ${compact ? 'compact' : ''}`} aria-label="Top hub signal">
      <div className="thsc-card-header">
        <div className="thsc-card-title-row">
          <TrendingUp size={18} className="thsc-icon" aria-hidden="true" />
          <h3 className="thsc-card-title">Top-Hub Signal</h3>
        </div>
        {insight.lastUpdated && (
          <span className="thsc-card-meta" title={insight.lastUpdated}>
            Updated {timeAgo(insight.lastUpdated)}
          </span>
        )}
      </div>

      <div className="thsc-hub-summary">
        <div className="thsc-hub-main">
          <span className="thsc-hub-name">{insight.topHub}</span>
          <span className="thsc-hub-count">{insight.connectionCount} connections</span>
        </div>
        {insight.description && <p className="thsc-hub-desc">{insight.description}</p>}
      </div>

      <div className="thsc-related-list">
        <div className="thsc-related-header">Top related docs</div>
        <ul className="thsc-docs-list" aria-label="Related documents">
          {docsToShow.map((doc, idx) => (
            <li key={idx} className="thsc-doc-item">
              <div className="thsc-doc-main">
                <a
                  href={doc.url || '#'}
                  className="thsc-doc-title"
                  target={doc.url ? '_blank' : undefined}
                  rel={doc.url ? 'noopener noreferrer' :
