# Costinel / frontend

## Decision: Highest-value incremental improvement (<2h)

**Chosen improvement:**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub (e.g., “MOC”) and returns the strongest cost-anomaly signal with full context. No writes, no state changes, safe to ship.

**Why this wins:**
- Directly applies pattern: *top-hub doc insight* (#knowledge-rag #graph #hub).
- Complements existing `GET /api/v1/cost-anomaly/signal/top` (if present) with hub-level context.
- Read-only, low-risk, and can be implemented in <2h (backend + minimal frontend card).
- Enables frontend to surface “Today’s top cost hub + anomaly” in the dashboard immediately.

---

## Implementation plan

1. **Backend** (`/opt/axentx/Costinel`)
   - Add route: `GET /api/v1/cost-anomaly/signal/top-hub`
   - Resolve today’s top hub via knowledge-rag query (use existing graph client / RAG helper).
   - Return shape:
     ```json
     {
       "hub": "MOC",
       "score": 0.94,
       "signal": "Unusual compute spend spike in us-east-1",
       "context": {
         "service": "EC2",
         "accounts": ["prod-01", "prod-02"],
         "deltaPct": 42,
         "window": "2026-05-02T00:00:00Z/2026-05-02T23:59:59Z"
       },
       "ts": "2026-05-02T22:45:00Z"
     }
     ```
   - If no hub or signal, return `204 No Content`.

2. **Frontend**
   - Add small dashboard card: **Top Hub Anomaly**.
   - Poll endpoint on mount + refresh button.
   - Show hub name, signal, severity badge, and short context list.
   - Link to detailed hub view (if available) or knowledge-rag page.

3. **Tests & validation**
   - Quick curl test against endpoint.
   - Verify UI renders without errors and handles empty state.

---

## Code snippets

### Backend route (Node/Express example)

```js
// routes/costAnomaly.js
const express = require('express');
const router = express.Router();
const { queryTopHub } = require('../services/knowledgeRag');

/**
 * GET /api/v1/cost-anomaly/signal/top-hub
 * Returns strongest cost-anomaly signal for today's top hub.
 */
router.get('/signal/top-hub', async (req, res) => {
  try {
    const result = await queryTopHub({
      date: new Date().toISOString().slice(0, 10), // YYYY-MM-DD
      domain: 'cost-anomaly',
      limit: 1
    });

    if (!result || !result.hub) {
      return res.status(204).end();
    }

    const payload = {
      hub: result.hub,
      score: result.score,
      signal: result.signal,
      context: {
        service: result.service,
        accounts: result.accounts,
        deltaPct: result.deltaPct,
        window: result.window
      },
      ts: new Date().toISOString()
    };

    res.json(payload);
  } catch (err) {
    console.error('[top-hub] query failed', err);
    res.status(500).json({ error: 'Unable to query top hub' });
  }
});

module.exports = router;
```

### Knowledge-rag service stub

```js
// services/knowledgeRag.js
const { getGraphClient } = require('./graphClient');

async function queryTopHub({ date, domain, limit = 1 }) {
  const g = getGraphClient();
  // Example query — adapt to your graph schema
  const query = `
    MATCH (h:Hub)-[r:HAS_SIGNAL]->(s:Signal {domain: $domain})
    WHERE date(s.windowStart) = date($date)
    RETURN h.name AS hub, r.score AS score,
           s.title AS signal, s.service AS service,
           s.accounts AS accounts, s.deltaPct AS deltaPct,
           s.windowStart + '/' + s.windowEnd AS window
    ORDER BY r.score DESC
    LIMIT $limit
  `;
  const result = await g.run(query, { date, domain, limit });
  if (!result.records.length) return null;
  const r = result.records[0];
  return {
    hub: r.get('hub'),
    score: r.get('score'),
    signal: r.get('signal'),
    service: r.get('service'),
    accounts: r.get('accounts'),
    deltaPct: r.get('deltaPct'),
    window: r.get('window')
  };
}

module.exports = { queryTopHub };
```

### Frontend card (React)

```tsx
// components/TopHubAnomalyCard.tsx
import { useEffect, useState } from 'react';
import axios from 'axios';

interface TopHubSignal {
  hub: string;
  score: number;
  signal: string;
  context: {
    service: string;
    accounts: string[];
    deltaPct: number;
    window: string;
  };
  ts: string;
}

export default function TopHubAnomalyCard() {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchSignal = async () => {
    setLoading(true);
    try {
      const res = await axios.get('/api/v1/cost-anomaly/signal/top-hub');
      setData(res.data);
    } catch (err: any) {
      if (err.response?.status !== 204) {
        console.error(err);
      }
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSignal();
  }, []);

  if (loading) return <div className="card">Loading top hub...</div>;
  if (!data) return <div className="card">No top hub anomaly detected today.</div>;

  const severity = data.score >= 0.8 ? 'critical' : data.score >= 0.5 ? 'warning' : 'info';

  return (
    <div className="card">
      <h3>Top Hub Anomaly</h3>
      <div className="hub-header">
        <strong>{data.hub}</strong>
        <span className={`badge ${severity}`}>{severity}</span>
      </div>
      <p className="signal">{data.signal}</p>
      <ul className="context">
        <li>Service: {data.context.service}</li>
        <li>Accounts: {data.context.accounts.join(', ')}</li>
        <li>Delta: +{data.context.deltaPct}%</li>
        <li>Window: {data.context.window}</li>
      </ul>
      <button onClick={fetchSignal} disabled={loading}>
        Refresh
      </button>
    </div>
  );
}
```

---

## Quick validation

```bash
# Start backend (if not running)
cd /opt/axentx/Costinel && npm run dev &

# Test endpoint
curl -s http://localhost:3000/api/v1/cost-anomaly/signal/top-hub | jq .
```

Expected: JSON payload or 204.

---

## Notes & follow-ups

- If knowledge-rag graph client isn’t wired yet, stub `queryTopHub` with a deterministic rule (e.g., query last 24h cost spikes by region/service and pick top) to unblock frontend.
- Ensure CORS allows frontend origin.
- Consider adding cache-control `max-age=60` since this is daily-level insight.
