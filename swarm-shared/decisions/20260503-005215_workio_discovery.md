# workio / discovery

## Final synthesized solution (best parts, resolved contradictions)

### 1. Diagnosis (merged)
- **Missing DB-level enforcement**: no partial unique constraint/index to guarantee “one open punch per user” (`clock_out_at IS NULL`), allowing duplicates under LINE webhook retries and concurrent requests.
- **No idempotency**: LINE webhook handler lacks an idempotency key; at-least-once delivery can insert duplicate punches within the same minute.
- **Non-transactional upsert**: clock-in path is not transactional, so concurrent retries can create two open rows for the same user/tenant.
- **No readiness probe**: missing `/readyz` prevents ops from confirming DB + external dependencies before routing traffic.
- **Missing fast lookup**: no index to enforce and quickly query the “one open punch” rule at scale.

### 2. Scope
- `workio/server/src/db/schema.sql` — add partial unique index and idempotency table.
- `workio/server/src/db/migrations/` — optional migration for existing deployments.
- `workio/server/src/controllers/punchController.ts` — transactional upsert with idempotency guard.
- `workio/server/src/routes/health.ts` (or `app.ts`) — add `/readyz` (and keep `/healthz`).

### 3. Implementation

#### 3.1 DB schema (constraint + index + idempotency)
Use **partial unique index** (not constraint) to support `tenant_id` scoping and allow concurrent creation safely. Add idempotency table with unique key and TTL cleanup.

```sql
-- workio/server/src/db/schema.sql

-- One open punch per user per tenant (fast + safe under concurrency)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_punches_open_per_user
  ON punches (tenant_id, user_id)
  WHERE (clock_out_at IS NULL);

-- Idempotency for LINE webhook deliveries
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_line_idempotency_created
  ON line_webhook_idempotency (created_at);
```

Optional cleanup job (run periodically):
```sql
DELETE FROM line_webhook_idempotency
WHERE created_at < NOW() - INTERVAL '24 hours';
```

#### 3.2 Punch controller — transactional upsert with idempotency
Key choices:
- Use a single transaction for idempotency check + punch upsert.
- Idempotency key: prefer `X-Line-Delivery-Id`; fallback to `X-Request-Id`.
- Hash payload to detect changed content for same delivery ID.
- Upsert pattern: try to close an existing open punch; if none, insert new punch. Rely on the partial unique index to prevent duplicates under race conditions.

```ts
// workio/server/src/controllers/punchController.ts
import { Request, Response } from 'express';
import { pool } from '../db';
import crypto from 'crypto';

function hashPayload(body: unknown): string {
  return crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
}

export async function handleLineWebhook(req: Request, res: Response) {
  const idempotencyKey = req.get('X-Line-Delivery-Id') || req.get('X-Request-Id');
  if (!idempotencyKey) {
    return res.status(400).json({ error: 'Missing idempotency key' });
  }

  const payloadHash = hashPayload(req.body);
  // Extract tenant/user from payload or auth context; adapt as needed
  const tenantId = req.body.tenantId || 'default-tenant';
  const userId = req.body.events?.[0]?.source?.userId;
  if (!userId) {
    return res.status(400).json({ error: 'Missing userId' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check (tenant-scoped)
    const idem = await client.query(
      `SELECT 1 FROM line_webhook_idempotency
       WHERE tenant_id = $1 AND idempotency_key = $2 AND payload_hash = $3`,
      [tenantId, idempotencyKey, payloadHash]
    );
    if (idem.rowCount && idem.rowCount > 0) {
      await client.query('ROLLBACK');
      return res.status(200).json({ ok: true, reason: 'duplicate' });
    }

    // Record idempotency
    await client.query(
      `INSERT INTO line_webhook_idempotency (tenant_id, idempotency_key, payload_hash)
       VALUES ($1, $2, $3)`,
      [tenantId, idempotencyKey, payloadHash]
    );

    const now = new Date();

    // Try to close an existing open punch for this user/tenant
    const closeRes = await client.query(
      `UPDATE punches
       SET clock_out_at = $1, updated_at = NOW()
       WHERE tenant_id = $2 AND user_id = $3 AND clock_out_at IS NULL
       RETURNING *`,
      [now, tenantId, userId]
    );

    // If there was an open punch, we closed it — optionally open a new one depending on policy.
    // For a simple toggle (clock-in/clock-out per webhook), return the closed punch.
    if (closeRes.rowCount && closeRes.rowCount > 0) {
      await client.query('COMMIT');
      return res.json({ ok: true, punch: closeRes.rows[0], action: 'clock-out' });
    }

    // No open punch: insert new clock-in
    const insertRes = await client.query(
      `INSERT INTO punches (tenant_id, user_id, clock_in_at, clock_out_at, created_at, updated_at)
       VALUES ($1, $2, $3, NULL, NOW(), NOW())
       RETURNING *`,
      [tenantId, userId, now]
    );

    await client.query('COMMIT');
    return res.json({ ok: true, punch: insertRes.rows[0], action: 'clock-in' });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook handling failed', err);
    return res.status(500).json({ error: 'Internal error' });
  } finally {
    client.release();
  }
}
```

Notes on behavior:
- The partial unique index guarantees at most one open punch per `(tenant_id, user_id)`. If concurrent inserts race, one will succeed and the other will error; the transaction will surface the error and rollback, and the idempotency record prevents silent duplicates on retry.
- If your policy is “each webhook toggles state” (clock-in → clock-out → clock-in), the above implements that. If you prefer “only one open punch and repeated triggers are no-ops,” remove the close-and-reinsert logic and instead upsert with `ON CONFLICT DO NOTHING` using the partial unique index.

#### 3.3 Health/readiness endpoint
Add a lightweight `/readyz` that checks DB connectivity (and optionally LINE token validity via a cheap cached check).

```ts
// workio/server/src/routes/health.ts
import { Router } from 'express';
import { pool } from '../db';

const router = Router();

router.get('/healthz', (_, res) => res.json({ status: 'ok' }));

router.get('/readyz', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    // Optional: add fast checks for LINE token or other critical deps here
    res.json({ status: 'ready', db: 'ok' });
  } catch (err) {
    res.status(503).json({ status: 'not ready', db: 'error', error: String(err) });
  }
});

export default router;
```

Wire into main app:
```ts
// workio/server/src/app.ts
import express from 'express';
import healthRoutes from './routes/health';
import lineWebhookRoute from './routes/lineWebhook';

const app = express();
app.use(express.json());
app.use('/webhook/line', lineWebhookRoute);
app.use('/', healthRoutes);

export default app;
```

### 4. Verification

- **DB enforcement**:
  - Confirm index exists: `\d punches
