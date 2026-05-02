# Costinel / frontend

### Final Synthesis — Deterministic, Read-Only “Strongest Anomaly” Widget

**Chosen improvement (≤2h):**  
Add a single, deterministic, read-only widget to the Costinel dashboard that calls `GET /api/v1/cost-anomaly/signal` and renders the strongest cost-anomaly signal as an actionable card (service, delta, context). No backend changes required.

**Why this wins:**  
- Uses existing backend contract (`/api/v1/cost-anomaly/signal`).  
- Read-only and deterministic (aligns with “Sense + Signal — ไม่ Execute”).  
- Highest UX leverage: puts the top anomaly in front of users immediately.  
- Can be shipped in <2 hours with tests and polish.

---

### Verified API Contract (use this)
Expect this exact shape from `GET /api/v1/cost-anomaly/signal`:
```json
{
  "service": "string",
  "delta": "+12.3%",
  "region": "string",
  "account": "string",
  "severity": "low|medium|high|critical",
  "description": "string",
  "timestamp": "ISO8601"
}
```

---

### Implementation Plan (timeboxed)

1. **Verify contract** (5m)  
   Confirm endpoint returns the shape above (use browser or `curl`).

2. **Create widget** `src/components/AnomalySignalCard.tsx` (40m)  
   - Fetch on mount with `useSWR` (or existing pattern).  
   - Deterministic empty/error states.  
   - Color by severity via semantic tokens.  
   - Read-only: no action buttons.

3. **Add to dashboard** `src/pages/Dashboard.tsx` (20m)  
   - Place as top-row card below header or in a “Signals” panel.  
   - Ensure mobile responsiveness.

4. **Loading & error UX** (15m)  
   - Skeleton shimmer while loading.  
   - Silent retry (2×) + inline error pill on failure.

5. **Polish & tests** (30m)  
   - Unit test mocking fetch.  
   - Storybook snapshot or smoke test.

6. **Verify & ship** (10m)  
   - Run dev build, confirm no regressions.

---

### Final Code

#### `src/components/AnomalySignalCard.tsx`
```tsx
import useSWR from 'swr';
import { useEffect, useState } from 'react';
import './AnomalySignalCard.css';

interface Signal {
  service: string;
  delta: string;
  region: string;
  account: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  description: string;
  timestamp: string;
}

const fetcher = (url: string) => fetch(url).then((r) => {
  if (!r.ok) throw new Error('Failed to fetch signal');
  return r.json();
});

export default function AnomalySignalCard() {
  const { data, error, isLoading, mutate } = useSWR<Signal>(
    '/api/v1/cost-anomaly/signal',
    fetcher,
    { refreshInterval: 60_000, revalidateOnFocus: false }
  );

  const [retryCount, setRetryCount] = useState(0);
  useEffect(() => {
    if (error && retryCount < 2) {
      const t = setTimeout(() => {
        mutate();
        setRetryCount((c) => c + 1);
      }, 3000);
      return () => clearTimeout(t);
    }
  }, [error, mutate, retryCount]);

  if (isLoading) {
    return <div className="signal-card skeleton" />;
  }

  if (error || !data) {
    return (
      <div className="signal-card signal-card--error" role="status">
        Unable to load anomaly signal
      </div>
    );
  }

  const severityClass = `signal-card--${data.severity}`;
  const isPositive = data.delta.startsWith('+');

  return (
    <div className={`signal-card ${severityClass}`}>
      <div className="signal-card__header">
        <span className="signal-card__badge">Anomaly</span>
        <span className={`signal-card__delta ${isPositive ? 'up' : 'down'}`}>
          {data.delta}
        </span>
      </div>
      <div className="signal-card__body">
        <strong>{data.service}</strong>
        <div className="signal-card__meta">
          {data.region} · {data.account}
        </div>
        <p className="signal-card__desc">{data.description}</p>
      </div>
      <div className="signal-card__footer">
        {new Date(data.timestamp).toLocaleString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        })}
      </div>
    </div>
  );
}
```

#### `src/components/AnomalySignalCard.css`
```css
.signal-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  background: var(--bg-card);
  color: var(--text);
  min-height: 110px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.signal-card.skeleton {
  background: linear-gradient(90deg, var(--bg-card) 25%, var(--border) 50%, var(--bg-card) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border: none;
}

@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.signal-card__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.signal-card__badge {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--accent);
  color: white;
}

.signal-card__delta {
  font-weight: 700;
  font-size: 16px;
}

.signal-card__delta.up {
  color: var(--danger);
}

.signal-card__delta.down {
  color: var(--success);
}

.signal-card__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.signal-card__meta {
  font-size: 13px;
  color: var(--muted);
}

.signal-card__desc {
  margin: 0;
  font-size: 14px;
  color: var(--text);
}

.signal-card__footer {
  margin-top: auto;
  font-size: 12px;
  color: var(--muted);
}

/* Severity accents */
.signal-card--low { border-left: 4px solid var(--info); }
.signal-card--medium { border-left: 4px solid var(--warning); }
.signal-card--high { border-left: 4px solid var(--danger); }
.signal-card--critical { border-left: 4px solid var(--danger); background: rgba(220, 53, 69, 0.06); }

.signal-card--error {
  color: var(--danger);
  text-align: center;
  display: flex;
  align-items: center;
  justify-content: center;
}
```

---

### Integration into Dashboard
Add to `src/pages/Dashboard.tsx`:
```tsx
import AnomalySignalCard from '../components/AnomalySignalCard';

export default function Dashboard() {
  return (
    <div className="dashboard">
      <header>...</header>
      <section className="dashboard__signals" style={{ marginBottom: 16 }}>
        <AnomalySignalCard />
      </section>
