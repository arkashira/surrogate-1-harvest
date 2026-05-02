# Costinel / quality

## Final Implementation Plan — Costinel Quality Increment (<2h)

**Chosen highest-value improvement:**  
Add a deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint that queries the knowledge graph for today’s top hub and returns the strongest cost-anomaly signal with full context.  
- No writes, no side effects, no cache mutations.  
- Backend-only change; no frontend coordination.  
- Complements existing `Sense + Signal` philosophy and the “top-hub doc insight” pattern (review most-connected hub before planning).

---

### Concrete implementation steps (≤2h)

1. **Add route**  
   Create `backend/src/routes/cost-anomaly/top-hub.ts` (or add to existing router) with:
   - `GET /api/v1/cost-anomaly/signal/top-hub`
   - Query param: `date?` (ISO date, defaults to today)
   - Returns `200` with `{ hub, signal, context }` or `204` if none.

2. **Implement read-only graph query**  
   Use existing knowledge-rag/graph client to:
   - Find today’s top hub (most-connected node for cost-anomaly signals on the date).
   - Retrieve the strongest outgoing cost-anomaly signal with full context (entity, metric, severity, time window, attribution).

3. **Add lightweight service layer**  
   Create `backend/src/services/costAnomalyTopHubService.ts`:
   - `getTopHubSignal(date): Promise<TopHubSignal | null>`
   - Deterministic: same date → same result (no random sampling).
   - No writes, no cache mutations, no external mutations.

4. **Wire route → service**  
   Inject service into route handler; return JSON.

5. **Add minimal tests**  
   - One unit test for service (mock graph).  
   - One integration test for route (200 shape).

6. **Verify no side effects**  
   Confirm no POST/PUT/DELETE calls, no DB writes, no external mutations.

---

### Code snippets

#### `backend/src/services/costAnomalyTopHubService.ts`
```ts
import { KnowledgeGraphClient } from '../lib/knowledgeGraphClient';

export interface TopHubSignal {
  hub: {
    id: string;
    label: string;
    type: string;
    connections: number;
  };
  signal: {
    id: string;
    type: string;
    severity: 'low' | 'medium' | 'high' | 'critical';
    entity: string;
    metric: string;
    value: number;
    baseline: number;
    deviation: number;
    window: {
      start: string; // ISO
      end: string;   // ISO
    };
  };
  context: {
    summary: string;
    recommendations: string[];
    tags: string[];
    attribution: {
      source: string;
      timestamp: string;
    };
  };
}

export class CostAnomalyTopHubService {
  constructor(private graphClient: KnowledgeGraphClient) {}

  async getTopHubSignal(dateISO?: string): Promise<TopHubSignal | null> {
    // Normalize date to YYYY-MM-DD for deterministic queries
    const date = dateISO
      ? new Date(dateISO).toISOString().split('T')[0]
      : new Date().toISOString().split('T')[0];

    // Deterministic query: top hub by connection count for cost-anomaly signals on this date
    const topHub = await this.graphClient.queryFirst<{
      hubId: string;
      hubLabel: string;
      hubType: string;
      connections: number;
    }>(`
      MATCH (h:Hub)-[:HAS_SIGNAL]->(s:CostAnomalySignal)
      WHERE date(s.windowStart) = date($date)
      WITH h, count(s) AS connections
      ORDER BY connections DESC
      LIMIT 1
      RETURN h.id AS hubId, h.label AS hubLabel, h.type AS hubType, connections
    `, { date });

    if (!topHub) return null;

    // Strongest signal from this hub for the same date
    // Use severityOrder and deviation magnitude for deterministic strongest selection
    const strongestSignal = await this.graphClient.queryFirst<{
      signalId: string;
      signalType: string;
      severity: string;
      entity: string;
      metric: string;
      value: number;
      baseline: number;
      deviation: number;
      windowStart: string;
      windowEnd: string;
      summary: string;
      recommendations: string[];
      tags: string[];
      source: string;
      ts: string;
    }>(`
      MATCH (h:Hub {id: $hubId})-[:HAS_SIGNAL]->(s:CostAnomalySignal)
      WHERE date(s.windowStart) = date($date)
      RETURN
        s.id AS signalId,
        s.type AS signalType,
        s.severity AS severity,
        s.entity AS entity,
        s.metric AS metric,
        s.value AS value,
        s.baseline AS baseline,
        s.deviation AS deviation,
        s.windowStart AS windowStart,
        s.windowEnd AS windowEnd,
        s.summary AS summary,
        s.recommendations AS recommendations,
        s.tags AS tags,
        s.source AS source,
        s.ts AS ts
      ORDER BY s.severityOrder DESC, abs(s.deviation) DESC
      LIMIT 1
    `, { hubId: topHub.hubId, date });

    if (!strongestSignal) return null;

    return {
      hub: {
        id: topHub.hubId,
        label: topHub.hubLabel,
        type: topHub.hubType,
        connections: topHub.connections,
      },
      signal: {
        id: strongestSignal.signalId,
        type: strongestSignal.signalType,
        severity: strongestSignal.severity,
        entity: strongestSignal.entity,
        metric: strongestSignal.metric,
        value: strongestSignal.value,
        baseline: strongestSignal.baseline,
        deviation: strongestSignal.deviation,
        window: {
          start: strongestSignal.windowStart,
          end: strongestSignal.windowEnd,
        },
      },
      context: {
        summary: strongestSignal.summary,
        recommendations: strongestSignal.recommendations,
        tags: strongestSignal.tags,
        attribution: {
          source: strongestSignal.source,
          timestamp: strongestSignal.ts,
        },
      },
    };
  }
}
```

#### `backend/src/routes/cost-anomaly/top-hub.ts`
```ts
import { Router } from 'express';
import { CostAnomalyTopHubService } from '../../services/costAnomalyTopHubService';
import { KnowledgeGraphClient } from '../../lib/knowledgeGraphClient';

const router = Router();
const graphClient = new KnowledgeGraphClient();
const service = new CostAnomalyTopHubService(graphClient);

/**
 * GET /api/v1/cost-anomaly/signal/top-hub
 * Query params:
 *   date? (optional) - ISO date string (YYYY-MM-DD). Defaults to today.
 *
 * Returns:
 *   200: { hub, signal, context }
 *   204: No signal found
 */
router.get('/api/v1/cost-anomaly/signal/top-hub', async (req, res) => {
  try {
    const date = req.query.date as string | undefined;
    const result = await service.getTopHubSignal(date);

    if (!result) {
      return res.status(204).send();
    }

    return res.status(200).json(result);
  } catch (error) {
    // Read-only endpoint: log and return 500 without side effects
    console.error('[Costinel] top-hub signal query failed:', error);
    return res.status(500).json({ error: 'Failed to query top-hub signal' });
  }
});

export default router;
```

#### Wire into main app (example)
In `backend/src/app.ts` (or wherever routes are registered):
```ts
import topHubRouter from './routes/cost-anomaly/top-hub';
app.use(topHubRouter);
```

---

### Verification checklist (quick)
- [ ] Route responds to `GET /api/v1/cost-anomaly/signal/top-hub` with correct params.  
- [ ] Returns `200
