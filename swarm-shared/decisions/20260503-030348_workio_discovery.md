# workio / discovery

## Final Synthesized Solution

### Diagnosis (merged, prioritized)
- **LINE webhook replays**: no `X-Line-Signature` verification and no idempotency layer allow duplicate processing.
- **Race conditions**: concurrent requests for same `(tenant_id, employee_id)` can create multiple open punches or double-close.
- **Missing DB constraints**: no uniqueness guard for open punches and no atomic state transition (read-then-write).
- **Missing raw-body handling**: signature verification requires unparsed request body.

### Scope (safe, incremental)
- Backend webhook handler + schema migration only. No client changes.

---

### Implementation

#### 1. Schema (`workio/server/src/db/schema.sql`)

```sql
-- Idempotency for LINE webhook deliveries (48h+ TTL recommended)
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_type      TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Optional cleanup (run via cron/TTL job)
-- DROP INDEX IF EXISTS idx_line_webhook_idempotency_created_at;
-- CREATE INDEX idx_line_webhook_idempotency_created_at ON line_webhook_idempotency(created_at);

-- At most one open punch per tenant+employee
-- Assumes punches has: id, tenant_id, employee_id, clock_in_at, clock_out_at
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_open_unique
  ON punches (tenant_id, employee_id)
  WHERE clock_out_at IS NULL;
```

#### 2. Webhook handler (`workio/server/src/routes/line.ts`)

```ts
import crypto from 'crypto';
import { Request, Response } from 'express';
import { pool } from '../db';

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET!;

function verifyLineSignature(rawBody: string, signature: string): boolean {
  if (!signature) return false;
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function generateIdempotencyKey(body: any): string {
  // Prefer stable platform-provided event ID; fallback to content hash.
  const events = Array.isArray(body.events) ? body.events : [];
  const first = events[0] || {};
  const stableId = first.webhookEventId || first.deliveryId || first.id || '';
  if (stableId) {
    return crypto.createHash('sha256').update(stableId).digest('hex');
  }
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
  const ts = first.timestamp || Date.now();
  const userId = (first.source && first.source.userId) || '';
  return crypto.createHash('sha256').update(`${ts}:${userId}:${payloadHash}`).digest('hex');
}

export async function lineWebhook(req: Request, res: Response) {
  const signature = req.headers['x-line-signature'] as string;
  const rawBody = typeof req.body === 'string' ? req.body : JSON.stringify(req.body);

  if (!verifyLineSignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
  const events = body.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).json({ error: 'No events' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      // Accept message/postback or other event types that should toggle punch
      if (!ev.type) continue;

      const idemKey = generateIdempotencyKey(body);
      const payloadHash = crypto.createHash('sha256').update(rawBody).digest('hex');

      // Idempotency check (within transaction)
      const idem = await client.query(
        `SELECT 1 FROM line_webhook_idempotency WHERE idempotency_key = $1`,
        [idemKey]
      );
      if (idem.rows.length > 0) {
        // Already processed; skip event but continue processing others
        continue;
      }

      // Record idempotency first
      await client.query(
        `INSERT INTO line_webhook_idempotency(idempotency_key, event_type, payload_hash)
         VALUES ($1, $2, $3)`,
        [idemKey, ev.type, payloadHash]
      );

      // Resolve employee by LINE user ID (adjust mapping column as needed)
      const lineUserId = ev.source && ev.source.userId;
      if (!lineUserId) continue;

      const emp = await client.query(
        `SELECT id, tenant_id FROM employees WHERE line_user_id = $1`,
        [lineUserId]
      );
      if (emp.rows.length === 0) continue;

      const employeeId = emp.rows[0].id;
      const tenantId = emp.rows[0].tenant_id;

      // Atomic punch transition:
      // - If open punch exists -> close it (clock_out_at = now)
      // - Else -> create open punch (clock_in_at = now)
      await client.query(`
        WITH open_punch AS (
          SELECT id FROM punches
          WHERE tenant_id = $1 AND employee_id = $2 AND clock_out_at IS NULL
          FOR UPDATE
        ),
        closed AS (
          UPDATE punches
          SET clock_out_at = NOW()
          WHERE id IN (SELECT id FROM open_punch)
          RETURNING id
        ),
        inserted AS (
          INSERT INTO punches (tenant_id, employee_id, clock_in_at, clock_out_at)
          SELECT $1, $2, NOW(), NULL
          WHERE NOT EXISTS (SELECT 1 FROM open_punch)
          RETURNING id
        )
        SELECT id FROM closed
        UNION ALL
        SELECT id FROM inserted
      `, [tenantId, employeeId]);
    }

    await client.query('COMMIT');
    return res.json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook error:', err);
    return res.status(500).json({ error: 'Internal error' });
  } finally {
    client.release();
  }
}
```

#### 3. Express wiring (raw body for signature)

```ts
import express from 'express';
import { lineWebhook } from './routes/line';

const app = express();

// Raw body for LINE signature verification
app.use('/webhook/line', express.raw({ type: 'application/json' }), (req, res, next) => {
  try {
    (req as any).body = JSON.parse(req.body.toString());
  } catch {
    return res.status(400).json({ error: 'Invalid JSON' });
  }
  next();
});
app.post('/webhook/line', lineWebhook);

// Normal JSON routes
app.use(express.json());
```

---

### Verification (concrete checks)

1. **Schema applied**
   ```bash
   psql $DATABASE_URL -c "\d line_webhook_idempotency"
   psql $DATABASE_URL -c "\d idx_punches_open_unique"
   ```

2. **Signature rejection**
   - POST to `/webhook/line` with missing/incorrect `X-Line-Signature` → 401.

3. **Idempotency**
   - Deliver same payload twice (same computed idempotency key) → second request must not create duplicate punch or idempotency row.
   - Verify `line_webhook_idempotency` contains one row and punch state is consistent.

4. **Race condition**
   - Concurrently POST two clock-in requests for same employee:
     ```js
     await Promise.all([post(), post()]);
     ```
   - Verify exactly one open punch exists:
     ```sql
     SELECT count(*) FROM punches
     WHERE tenant_id = ? AND employee_id = ? AND clock_out_at IS NULL;
     ```

5. **Open-punch uniqueness**
   - Attempt to insert a second open punch for same `(tenant_id, employee_id)` → DB constraint violation (handled gracefully by app).
