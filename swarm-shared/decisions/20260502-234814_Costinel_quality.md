# Costinel / quality

## Final Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Highest-value, read-only frontend card (<2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (no self-execution, no writes)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with a score, concise insight, and top signals so users see top context before acting.

---

### 1) Architecture (read-only)

- **Frontend** (`React + TypeScript`): new card `TopHubSignalCard`
  - Polls `GET /api/v1/cost-anomaly/signal/top-hub` on mount + every 60s (SWR-like)
  - Shows: hub name, score badge, short insight, top signals (max 3, expandable), last updated
  - Skeleton while loading; inline error with retry; no side effects or mutations
- **Backend** (`FastAPI`): minimal read-only endpoint returning deterministic mock for now
  - Later: swap mock for real `knowledge-rag`/graph query with short-TTL cache
- **No DB writes** — strictly sense + signal

---

### 2) File changes

#### `src/api/index.ts` — typed client call

```ts
export interface SignalItem {
  id: string;
  title: string;
  severity: 'critical' | 'warning' | 'info';
  context?: string;
}

export interface TopHubSignal {
  hubId: string;
  hubName: string;
  score: number;
  insight: string;
  signals: SignalItem[];
  lastUpdated: string; // ISO
}

export async function fetchTopHubSignal(): Promise<TopHubSignal> {
  const res = await fetch('/api/v1/cost-anomaly/signal/top-hub', {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  });
  if (!res.ok) throw new Error('Failed to fetch top-hub signal');
  return res.json();
}
```

#### `src/components/dashboard/TopHubSignalCard.tsx`

```tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal, TopHubSignal, SignalItem } from '../../api';
import './TopHubSignalCard.css';

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444',
  warning: '#f59e0b',
  info: '#3b82f6',
};

export function TopHubSignalCard() {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const load = () => {
    fetchTopHubSignal()
      .then(setSignal)
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, []);

  if (error) {
    return (
      <div className="top-hub-card card error">
        <span>Signal unavailable</span>
        <button onClick={load}>Retry</button>
      </div>
    );
  }

  if (!signal) {
    return (
      <div className="top-hub-card card loading">
        <div className="skeleton hub"></div>
        <div className="skeleton score"></div>
        <div className="skeleton insight"></div>
        <div className="skeleton signals"></div>
      </div>
    );
  }

  const visibleSignals = expanded
    ? signal.signals
    : signal.signals.slice(0, 3);

  return (
    <div className="top-hub-card card">
      <div className="header">
        <span className="label">Top Hub</span>
        <span className="updated">
          {new Date(signal.lastUpdated).toLocaleTimeString()}
        </span>
      </div>

      <div className="hub-row">
        <div className="hub">{signal.hubName}</div>
        <div className="score-badge">{signal.score.toFixed(1)}</div>
      </div>

      <div className="insight">{signal.insight}</div>

      {signal.signals.length > 0 && (
        <div className="signals">
          {visibleSignals.map((s) => (
            <div key={s.id} className="signal-row">
              <span
                className="dot"
                style={{ background: SEVERITY_COLORS[s.severity] }}
              />
              <span className="title">{s.title}</span>
            </div>
          ))}
          {signal.signals.length > 3 && (
            <button
              className="toggle-btn"
              onClick={() => setExpanded((x) => !x)}
            >
              {expanded ? 'Show less' : `+${signal.signals.length - 3} more`}
            </button>
          )}
        </div>
      )}

      <div className="footer">
        <small>Sense + Signal — ไม่ Execute</small>
      </div>
    </div>
  );
}
```

#### `src/components/dashboard/TopHubSignalCard.css`

```css
.top-hub-card {
  padding: 16px;
  border-radius: 8px;
  background: #0f172a;
  color: #e2e8f0;
  min-width: 280px;
  max-width: 360px;
  border: 1px solid #1e293b;
}

.top-hub-card .header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: #94a3b8;
  margin-bottom: 8px;
}

.top-hub-card .hub-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
}

.top-hub-card .hub {
  font-size: 18px;
  font-weight: 700;
  color: #10b981;
}

.top-hub-card .score-badge {
  font-size: 14px;
  font-weight: 600;
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.1);
  padding: 2px 8px;
  border-radius: 999px;
}

.top-hub-card .insight {
  font-size: 13px;
  color: #cbd5e1;
  line-height: 1.4;
  margin-bottom: 8px;
}

.top-hub-card .signals {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}

.top-hub-card .signal-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12px;
  color: #cbd5e1;
}

.top-hub-card .signal-row .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}

.top-hub-card .signal-row .title {
  word-break: break-word;
}

.top-hub-card .toggle-btn {
  background: none;
  border: none;
  color: #60a5fa;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  text-align: left;
}

.top-hub-card .footer {
  margin-top: 8px;
  font-size: 11px;
  color: #64748b;
}

.top-hub-card.error {
  border-left: 4px solid #ef4444;
}

.top-hub-card.loading .skeleton {
  background: #1e293b;
  border
