# workio / discovery

## Final synthesized answer (best parts, resolved contradictions, concrete + actionable)

We adopt **Candidate 1’s reliability core** (idempotency, durable outbound queue, exponential backoff, safe retries) and **Candidate 2’s discoverability/core-validation core** (machine-readable API surface, startup checks, local bootstrap). We resolve contradictions by making discovery **non-blocking and read-only**, while clock/LINE delivery remains **async and durable**.

---

### 1. Diagnosis (merged)

- **Reliability**: transient LINE failures, synchronous notify calls, duplicate clock events, missing idempotency, no retry/backoff, no observability.
- **Discoverability/ops**: no OpenAPI, no route map, no startup validation of LINE/webhook/credentials, no lightweight dashboard or bootstrap script.
- **Contradiction to resolve**: Candidate 1 pushes async queue for everything; Candidate 2 implies synchronous checks/discovery. We resolve by keeping **user-facing clock writes fast and async**, while **startup validation and discovery endpoints are synchronous/read-only** and never block user flows.

---

### 2. Proposed change (merged)

Add:

- **Idempotent, durable outbound queue** for LINE notifications and clock events.
- **Idempotency middleware** keyed per tenant.
- **Discovery module**: OpenAPI, route index, health+capability snapshot, startup checks, bootstrap script.
- **Minimal ops surface**: recent-events tail and queue status endpoint (read-only) for quick diagnosis.

Files to add/modify:

- Add:  
  - `server/src/queue/lineDeliveryQueue.ts`  
  - `server/src/middleware/idempotency.ts`  
  - `server/src/routes/discovery.ts`  
  - `server/src/routes/openapi.ts` (or generate via tsoa/zod-to-openapi)  
  - `server/src/startup/validateEnv.ts`  
  - `scripts/bootstrap-local.sh`  
  - `docs/API.md` (auto-generated)

- Modify:  
  - `server/src/routes/clock.ts`  
  - `server/src/services/lineService.ts`  
  - `server/src/db/schema.sql`  
  - `server/src/app.ts`

---

### 3. Implementation (merged + corrected)

#### 3.1 DB schema (additions)

```sql
-- server/src/db/schema.sql
CREATE TABLE IF NOT EXISTS clock_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  user_id UUID NOT NULL REFERENCES users(id),
  type VARCHAR(10) NOT NULL CHECK (type IN ('in', 'out')),
  latitude DECIMAL(9,6),
  longitude DECIMAL(9,6),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  idempotency_key VARCHAR(128) NOT NULL,
  UNIQUE(tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS line_delivery_queue (
  id BIGSERIAL PRIMARY KEY,
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  user_id UUID NOT NULL REFERENCES users(id),
  payload_type VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  next_attempt_at TIMESTAMPTZ DEFAULT NOW(),
  status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','succeeded','failed')),
  error_message TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_line_delivery_queue_next ON line_delivery_queue(next_attempt_at, status) WHERE status = 'pending';
CREATE INDEX idx_clock_events_idempotency ON clock_events(tenant_id, idempotency_key);
```

#### 3.2 Idempotency middleware (unchanged from Candidate 1, safe)

```ts
// server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';
import { db } from '../db';

export async function idempotencyGuard(req: Request, res: Response, next: NextFunction) {
  const key = req.headers['x-idempotency-key'] as string;
  if (!key) return res.status(400).json({ error: 'x-idempotency-key required' });

  const tenantId = (req as any).tenantId;
  const exists = await db.oneOrNone(
    `SELECT id FROM clock_events WHERE tenant_id = $1 AND idempotency_key = $2`,
    [tenantId, key]
  );

  if (exists) return res.status(409).json({ error: 'Duplicate request', idempotency_key: key });

  (req as any).idempotencyKey = key;
  next();
}
```

#### 3.3 Durable LINE delivery queue (corrected + safe)

```ts
// server/src/queue/lineDeliveryQueue.ts
import { db } from '../db';
import { sendLineMessage } from '../services/lineService';

export async function enqueueLineDelivery(tenantId: string, userId: string, payloadType: string, payload: any) {
  await db.none(
    `INSERT INTO line_delivery_queue(tenant_id, user_id, payload_type, payload)
     VALUES($1,$2,$3,$4)`,
    [tenantId, userId, payloadType, payload]
  );
}

export async function processLineDeliveries(signal?: AbortSignal) {
  while (!signal?.aborted) {
    try {
      const rows = await db.tx(async (t) => {
        const batch = await t.any<{
          id: number;
          tenant_id: string;
          user_id: string;
          payload: any;
          attempts: number;
        }>(
          `UPDATE line_delivery_queue
           SET attempts = attempts + 1,
               last_attempt_at = NOW(),
               next_attempt_at = NOW() + (power(2, attempts) * interval '1 second')
           WHERE id IN (
             SELECT id FROM line_delivery_queue
             WHERE status = 'pending' AND next_attempt_at <= NOW()
             ORDER BY next_attempt_at
             FOR UPDATE SKIP LOCKED
             LIMIT 20
           )
           RETURNING id, tenant_id, user_id, payload, attempts`
        );
        return batch;
      });

      for (const row of rows) {
        await sendWithRetry({ ...row });
      }
    } catch (err) {
      console.error('[line-queue] processing error', err);
    }

    await new Promise((r) => setTimeout(r, 2000));
  }
}

async function sendWithRetry(item: { id: number; tenant_id: string; user_id: string; payload: any; attempts: number }) {
  try {
    await sendLineMessage(item.tenant_id, item.user_id, item.payload);
    await db.none(`UPDATE line_delivery_queue SET status='succeeded', updated_at=NOW() WHERE id=$1`, [item.id]);
  } catch (err: any) {
    const status = err?.response?.status;
    const isRetryable = !status || status >= 500 || status === 429;
    const maxAttempts = 12;

    if (!isRetryable || item.attempts >= maxAttempts) {
      await db.none(
        `UPDATE line_delivery_queue SET status='failed', error_message=$1, updated_at=NOW() WHERE id=$2`,
        [err?.message || 'Unknown error', item.id]
      );
    }
    // else: exponential backoff already set; will retry on next poll
  }
}
```

#### 3.4 Clock route (non-blocking, idempotent)

```ts
// server/src/routes/clock.ts
import express from 'express';
import { db } from '../db';
import { idempotencyGuard } from '../middleware/idempotency';
import { enqueueLineDelivery } from '../queue/lineDeliveryQueue';

const router = express.Router();

router.post('/clock', idempotencyGuard, async (req, res) => {
  const { type, latitude, longitude } = req.body;
  const userId = (req as any).userId;
  const tenantId = (req as any).tenantId;
  const idempotencyKey = (req as any).idempotencyKey;

  const result = await db.tx(async (t) => {
    const event = await t.one(

