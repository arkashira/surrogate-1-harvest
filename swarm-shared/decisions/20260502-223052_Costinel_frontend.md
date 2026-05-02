# Costinel / frontend

## Implementation Plan — Costinel Frontend (≤2h)

**Highest-value incremental improvement**  
Add a read-only `GET /api/v1/cost-anomaly/signal` frontend integration that surfaces today’s top cost anomaly as a dismissible signal card on the dashboard. This delivers immediate governance value (Sense + Signal) without execute permissions.

**Scope (frontend only)**
- Add `src/services/anomalyService.js` (axios wrapper)
- Add `src/components/AnomalySignalCard/AnomalySignalCard.jsx` + styles
- Wire into `src/pages/Dashboard/Dashboard.jsx`
- Mock contract matches backend spec (deterministic, today-only, single highest-cost anomaly)
- LocalStorage dismiss for session (no backend writes)

**Estimated effort**: ~90 minutes

---

## Code Snippets

### 1) Service: `src/services/anomalyService.js`
```js
import axios from 'axios';

const API_BASE = '/api/v1';

export const getTodayTopAnomaly = async () => {
  // GET /api/v1/cost-anomaly/signal
  // Expected 200: { id, timestamp, service, region, account, metric, value, currency, severity, description }
  // Expected 204: no anomaly today
  const res = await axios.get(`${API_BASE}/cost-anomaly/signal`, {
    // read-only; no auth escalation
    timeout: 8000,
  });

  if (res.status === 204) return null;
  return res.data;
};
```

### 2) Component: `src/components/AnomalySignalCard/AnomalySignalCard.jsx`
```jsx
import React from 'react';
import './AnomalySignalCard.scss';

const SEVERITY_ICON = {
  low: '⚠️',
  medium: '🔶',
  high: '🔥',
  critical: '🚨',
};

export const AnomalySignalCard = ({ anomaly, onDismiss }) => {
  if (!anomaly) return null;

  const icon = SEVERITY_ICON[anomaly.severity] || SEVERITY_ICON.medium;
  const value = Number(anomaly.value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  return (
    <div className="anomaly-signal-card" role="alert" aria-live="polite">
      <div className="anomaly-signal-card__header">
        <span className="anomaly-signal-card__icon">{icon}</span>
        <span className="anomaly-signal-card__title">Cost Anomaly Detected</span>
        <button
          className="anomaly-signal-card__close"
          onClick={onDismiss}
          aria-label="Dismiss signal"
        >
          ×
        </button>
      </div>

      <div className="anomaly-signal-card__body">
        <div className="anomaly-signal-card__main">
          <strong>{anomaly.service}</strong> in <em>{anomaly.region}</em> /{' '}
          <em>{anomaly.account}</em>
        </div>

        <div className="anomaly-signal-card__metric">
          {anomaly.metric}: {value} {anomaly.currency}
        </div>

        {anomaly.description && (
          <p className="anomaly-signal-card__description">{anomaly.description}</p>
        )}

        <div className="anomaly-signal-card__meta">
          {new Date(anomaly.timestamp).toLocaleString()}
        </div>
      </div>
    </div>
  );
};
```

### 3) Styles: `src/components/AnomalySignalCard/AnomalySignalCard.scss`
```scss
.anomaly-signal-card {
  border: 1px solid #f5c64c;
  background: #fff8e1;
  color: #5d4037;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);

  &__header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }

  &__icon {
    font-size: 18px;
  }

  &__title {
    font-weight: 600;
    flex: 1;
  }

  &__close {
    background: none;
    border: none;
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
    color: #8d6e63;
  }

  &__body {
    font-size: 14px;
  }

  &__main {
    margin-bottom: 6px;
  }

  &__metric {
    font-weight: 600;
    margin-bottom: 6px;
  }

  &__description {
    margin: 6px 0;
    color: #5d4037;
    opacity: 0.9;
  }

  &__meta {
    margin-top: 8px;
    font-size: 12px;
    color: #8d6e63;
    opacity: 0.8;
  }
}
```

### 4) Dashboard Integration: `src/pages/Dashboard/Dashboard.jsx`
```jsx
import React, { useEffect, useState } from 'react';
import { getTodayTopAnomaly } from '../../services/anomalyService';
import { AnomalySignalCard } from '../../components/AnomalySignalCard/AnomalySignalCard';

export const Dashboard = () => {
  const [anomaly, setAnomaly] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        // session-level dismiss stored locally
        const dismissed = sessionStorage.getItem('costinel_anomaly_dismissed_today');
        if (dismissed) {
          setLoading(false);
          return;
        }

        const data = await getTodayTopAnomaly();
        setAnomaly(data);
      } catch (err) {
        // read-only: fail silently but log for ops
        // eslint-disable-next-line no-console
        console.warn('Costinel anomaly signal unavailable', err);
      } finally {
        setLoading(false);
      }
    };

    load();
  }, []);

  const handleDismiss = () => {
    sessionStorage.setItem('costinel_anomaly_dismissed_today', '1');
    setAnomaly(null);
  };

  return (
    <div className="dashboard">
      <h1>Cost Dashboard</h1>

      {/* Other dashboard widgets... */}

      {!loading && (
        <AnomalySignalCard
          anomaly={anomaly}
          onDismiss={handleDismiss}
        />
      )}

      {/* Rest of dashboard */}
    </div>
  );
};
```

---

## Acceptance Criteria (read-only)
- `GET /api/v1/cost-anomaly/signal` returns 200 with single highest-cost anomaly for today or 204 if none.
- Frontend shows dismissible signal card on dashboard when anomaly exists.
- Dismiss persists for session via `sessionStorage` (no backend writes).
- No execute actions exposed; strictly Sense + Signal.
- Graceful degradation if endpoint unavailable.
