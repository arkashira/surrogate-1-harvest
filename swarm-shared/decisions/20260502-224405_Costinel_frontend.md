# Costinel / frontend

## Final Implementation Plan  
**Highest-value incremental improvement (<2h):**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint (backend stub/proxy to existing graph service) and a **Top Anomaly Signal Panel** in the dashboard that consumes it.  

- **Why this now**  
  - Completes `Costinel = Sense + Signal (ไม่ Execute)` by surfacing the strongest anomaly for human review.  
  - Aligns with knowledge-rag + top-hub pattern (review most-connected hub first).  
  - Reuses existing graph/RAG pipeline; no training/infra changes; avoids HF API/rate-limit issues.  
  - Frontend-only UI surface; backend is a thin contract + cache-safe query (no side effects).  

---

### 1) Backend contract (read-only, deterministic)  
`GET /api/v1/cost-anomaly/signal/top-hub`  

Query params:  
- `date=YYYY-MM-DD` (default today)  
- `hub=string` (optional; defaults to top hub by degree/centrality)  

Response (200):  
```json
{
  "hub": "MOC",
  "signal": {
    "id": "cost-anomaly-2026-05-03-aws-ec2-spike",
    "type": "cost-anomaly",
    "severity": "high",
    "score": 0.92,
    "title": "AWS EC2 spend spike in us-east-1",
    "description": "Detected 3.4x baseline increase in EC2 on-demand spend for account 123456789012.",
    "context": {
      "accounts": ["123456789012"],
      "regions": ["us-east-1"],
      "services": ["EC2"],
      "timeRange": "2026-05-03T00:00:00Z/2026-05-03T23:59:59Z",
      "baseline": 1200.00,
      "current": 4080.00,
      "unit": "USD"
    },
    "recommendations": [
      "Check for runaway instances or scheduled task bursts.",
      "Validate RI/SP coverage for affected account/region."
    ],
    "proposal": {
      "id": "prop-20260503-001",
      "title": "Investigate EC2 spend spike",
      "actions": ["create-jira", "notify-finance"]
    },
    "ts": "2026-05-03T10:12:00Z"
  }
}
```

Behavior:  
- No writes, no side effects.  
- Cache-friendly: set `Cache-Control: public, max-age=60, stale-while-revalidate=300` (or similar) to protect graph queries.  
- On transient graph/backend errors, return 204 No Content or 200 with `null` payload (graceful degradation).  

---

### 2) Frontend service layer (robust + cache-aware)  
`src/services/topHubSignal.js`  

```javascript
// src/services/topHubSignal.js
const API_BASE = process.env.REACT_APP_API_BASE || '/api/v1';
const CACHE_TTL_MS = 5 * 60 * 1000;

let cached = null;
let cachedAt = 0;

export async function fetchTopHubSignal({ date, hub } = {}) {
  const now = Date.now();
  if (cached && now - cachedAt < CACHE_TTL_MS) return cached;

  const params = new URLSearchParams();
  if (date) params.set('date', date);
  if (hub) params.set('hub', hub);

  const url = `${API_BASE}/cost-anomaly/signal/top-hub${params.toString() ? '?' + params.toString() : ''}`;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);

  try {
    const res = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
      credentials: 'same-origin'
    });
    clearTimeout(timeout);

    if (!res.ok || res.status === 204) {
      cached = null;
      return null;
    }

    const payload = await res.json();
    cached = payload;
    cachedAt = now;
    return payload;
  } catch (err) {
    clearTimeout(timeout);
    cached = null;
    return null;
  }
}
```

---

### 3) React hook (polling + SSR-safe)  
`src/hooks/useTopHubSignal.js`  

```javascript
// src/hooks/useTopHubSignal.js
import { useEffect, useState, useCallback, useRef } from 'react';
import { fetchTopHubSignal } from '../services/topHubSignal';

export function useTopHubSignal({ pollInterval = 300000, enabled = true } = {}) {
  const [signal, setSignal] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Prevent double-load on mount when caller rapidly toggles enabled
  const enabledRef = useRef(enabled);

  const load = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      const result = await fetchTopHubSignal();
      setSignal(result);
      setError(null);
    } catch (err) {
      setError(err);
      setSignal(null);
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    // If enabled flips rapidly, avoid duplicate loads
    if (enabled === enabledRef.current) {
      load();
    } else {
      enabledRef.current = enabled;
      // still load on enable change
      load();
    }

    if (!enabled || pollInterval <= 0) return;
    const id = setInterval(load, pollInterval);
    return () => clearInterval(id);
  }, [load, pollInterval, enabled]);

  return { signal, loading, error, refetch: load };
}
```

---

### 4) Presentational component (dashboard card)  
`src/components/CostAnomaly/TopAnomalySignalPanel.tsx`  

```tsx
// src/components/CostAnomaly/TopAnomalySignalPanel.tsx
import React from 'react';
import { useTopHubSignal } from '../../hooks/useTopHubSignal';
import './TopAnomalySignalPanel.css';

export function TopAnomalySignalPanel({ compact = false, pollInterval = 300000 }) {
  const { signal, loading, error } = useTopHubSignal({ pollInterval, enabled: true });

  if (loading && !signal) {
    return <div className="top-anomaly-card loading">Loading signal…</div>;
  }

  if (error && !signal) {
    return null; // silent degradation
  }

  if (!signal?.signal) {
    return null;
  }

  const { hub, signal: s } = signal;
  const severityColor = s.severity === 'high' ? '#d32f2f' : s.severity === 'medium' ? '#f57c00' : '#388e3c';

  if (compact) {
    return (
      <div className="top-anomaly-card compact" style={{ borderLeftColor: severityColor }}>
        <strong>{hub}</strong> · {s.title}
      </div>
    );
  }

  return (
    <div className="top-anomaly-card" style={{ borderLeftColor: severityColor }}>
      <div className="top-anomaly-card__header">
        <span className="top-anomaly-card__hub">Top hub: {hub}</span>
        <span className="top-anomaly-card__severity" style={{ color: severityColor }}>
          {s.severity.toUpperCase()}
        </span>
      </div>
      <h4 className="top-anomaly-card
