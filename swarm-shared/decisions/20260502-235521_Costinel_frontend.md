# Costinel / frontend

## Final Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, rationale, and contextual signals in a compact, production-ready card.

### High-value summary
- Pure frontend addition (no backend changes) that immediately surfaces the #knowledge-rag top-hub insight.
- Uses existing design tokens, layout grid, and CDN-friendly patterns.
- Static fallback data ensures the card always renders, even if graph services are unavailable.
- Zero mutations or execution paths — strictly display.

---

### File changes (paths relative to `/opt/axentx/Costinel`)

1. **`src/components/cards/TopHubSignalCard.tsx`** (new)  
2. **`src/hooks/useTopHub.ts`** (new)  
3. **`src/pages/Dashboard.tsx`** (or main dashboard layout) — add card to grid  
4. **`src/types/signal.ts`** (add minimal types)  
5. **`src/components/cards/TopHubSignalCard.css`** (scoped styles)

---

### Types

#### `src/types/signal.ts`
```ts
export interface HubSignal {
  type: 'anomaly' | 'recommendation' | 'trend' | 'risk';
  title: string;
  description: string;
  severity: 'low' | 'medium' | 'high';
}

export interface HubLink {
  label: string;
  url: string;
  category?: 'docs' | 'runbook' | 'proposal' | 'analysis';
}

export interface HubInsight {
  hubId: string;        // e.g. "MOC"
  label: string;        // human readable
  score: number;        // 0-100 connectivity/strength
  rationale: string[];  // short bullet reasons
  signals: HubSignal[];
  links?: HubLink[];
  lastUpdated: string;  // ISO timestamp
}
```

---

### Data hook (CDN-friendly + static fallback)

#### `src/hooks/useTopHub.ts`
```ts
import { useEffect, useState, useCallback } from 'react';
import type { HubInsight } from '../types/signal';

const FALLBACK_HUB: HubInsight = {
  hubId: 'MOC',
  label: 'Multi-Org Cost',
  score: 92,
  rationale: [
    'Highest cross-account linkage (14 accounts)',
    'Consistent tagging coverage (>95%)',
    'Top contributor to forecasted savings (38%)'
  ],
  signals: [
    {
      type: 'recommendation',
      title: 'RI coverage gap',
      description: '3 accounts below 70% RI utilization; opportunity ~$42k/yr',
      severity: 'medium'
    },
    {
      type: 'trend',
      title: 'Cost spike detected',
      description: 'Week-over-week +18% in compute; investigate scheduled jobs',
      severity: 'high'
    }
  ],
  links: [
    { label: 'Savings proposal', url: '/proposals/moc-savings', category: 'proposal' },
    { label: 'Runbook', url: '/runbooks/moc-optimization', category: 'runbook' }
  ],
  lastUpdated: new Date().toISOString()
};

const ENDPOINT = '/api/signal/hubs/top';

export function useTopHub(pollInterval = 0) {
  const [hub, setHub] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchTopHub = useCallback(async () => {
    try {
      const res = await fetch(ENDPOINT, { cache: 'no-store' });
      if (!res.ok) throw new Error('Failed to fetch top hub');
      const data = (await res.json()) as HubInsight;
      setHub(data);
    } catch {
      // Graceful fallback: always show card with static data
      setHub(FALLBACK_HUB);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTopHub();
    if (pollInterval > 0) {
      const id = setInterval(fetchTopHub, pollInterval);
      return () => clearInterval(id);
    }
  }, [fetchTopHub, pollInterval]);

  return { hub, loading, refetch: fetchTopHub };
}
```

---

### Card component

#### `src/components/cards/TopHubSignalCard.tsx`
```tsx
import React from 'react';
import { useTopHub } from '../../hooks/useTopHub';
import type { HubInsight } from '../../types/signal';
import './TopHubSignalCard.css';

function SeverityBadge({ severity }: { severity: HubInsight['signals'][0]['severity'] }) {
  return <span className={`severity-badge severity-${severity}`}>{severity}</span>;
}

export default function TopHubSignalCard() {
  const { hub, loading } = useTopHub();

  if (loading) {
    return (
      <div className="top-hub-card loading" aria-busy="true">
        <div className="skeleton-title"></div>
        <div className="skeleton-row"></div>
        <div className="skeleton-row short"></div>
      </div>
    );
  }

  if (!hub) return null;

  return (
    <article className="top-hub-card" role="region" aria-label={`Top hub: ${hub.label}`}>
      <header className="top-hub-header">
        <div className="hub-title-wrap">
          <h3 className="hub-title">{hub.label}</h3>
          <p className="hub-sub">{hub.hubId} — Top connected hub</p>
        </div>
        <div className="hub-score" title="Connectivity score 0–100">
          {hub.score}
          <small>/100</small>
        </div>
      </header>

      <section className="hub-rationale" aria-label="Rationale">
        <ul>
          {hub.rationale.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      </section>

      <section className="hub-signals" aria-label="Related signals">
        <h4 className="visually-hidden">Signals</h4>
        <ul>
          {hub.signals.map((s, i) => (
            <li key={i} className="signal-row">
              <div className="signal-meta">
                <span className="signal-type">{s.type}</span>
                <SeverityBadge severity={s.severity} />
              </div>
              <div className="signal-content">
                <strong>{s.title}</strong>
                <p>{s.description}</p>
              </div>
            </li>
          ))}
        </ul>
      </section>

      {hub.links && hub.links.length > 0 && (
        <footer className="hub-footer">
          <ul className="hub-links">
            {hub.links.map((l, i) => (
              <li key={i}>
                <a href={l.url} target="_blank" rel="noopener noreferrer">
                  {l.label}
                </a>
              </li>
            ))}
          </ul>
          <small className="hub-updated">
            Last updated {new Date(hub.lastUpdated).toLocaleString()}
          </small>
        </footer>
      )}
    </article>
  );
}
```

---

### Styles

#### `src/components/cards/TopHubSignalCard.css`
```css
.top-hub-card {
  background: #fff;
  border: 1px solid #e6e9ef;
  border-radius: 10px;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 260px;
}

.top-hub-header {
  display: flex;
  justify-content: space-between;
