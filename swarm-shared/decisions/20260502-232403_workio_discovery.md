# workio / discovery

## Final consolidated solution (correct + actionable)

**Core problem**: LINE webhook redeliveries and concurrent deliveries create duplicate punch rows because there is no durable idempotency key and no DB-level uniqueness.

**Chosen approach** (resolve contradictions in favor of correctness + actionability):
- Use a **stable LINE delivery/message ID** as the idempotency key (not a synthetic composite) because it is the only value guaranteed by LINE to identify a specific delivery.
- Enforce uniqueness **at the DB level** with a nullable unique index on that column (`line_message_id`) so duplicates are rejected even under concurrency.
- Make punch creation **atomic** with `ON CONFLICT DO NOTHING` inside a transaction that also records a delivery log for audit/debuggability.
- Keep business logic for clock-in/out determination inside the same transaction to avoid races.
- Add lightweight observability (structured logs + metrics) so duplicate suppression is visible in production.

---

### 1) Schema changes

File: `/opt/axentx/workio/server/src/db/schema.sql`

```sql
-- Idempotency key for LINE webhook deliveries
ALTER TABLE punches ADD COLUMN IF NOT EXISTS line_message_id VARCHAR(255);
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_message_id
  ON punches (line_message_id)
  WHERE line_message_id IS NOT NULL;

-- Optional domain-level safety (if you want to prevent double punches by business rule)
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_tenant_user_date_shift
--   ON punches (tenant_id, user_id, punch_date, shift_type)
--   WHERE deleted_at IS NULL;

-- Durable LINE delivery event log (audit + replay detection)
CREATE TABLE IF NOT EXISTS line_delivery_events (
  id BIGSERIAL PRIMARY KEY,
  line_delivery_id VARCHAR(255) NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id),
  event_type VARCHAR(50) NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  processed_at TIMESTAMPTZ,
  UNIQUE (line_delivery_id)
);
```

---

### 2) Idempotent LINE webhook handler

File: `/opt/axentx/workio/server/src/routes/line-webhook.ts`

```ts
import { Router, Request, Response } from 'express';
import { pool } from '../db';
import { verifyLineSignature } from '../utils/line';
import { Counter, Histogram, Registry } from 'prom-client'; // optional observability

const router = Router();

// Optional metrics
const webhookReceived = new Counter({
  name: 'line_webhook_received_total',
  help: 'Total LINE webhook requests received',
  labelNames: ['status'],
  registers: [new Registry()],
});
const duplicateSuppressed = new Counter({
  name: 'line_webhook_duplicate_suppressed_total',
  help: 'Total duplicate LINE deliveries suppressed',
  registers: [new Registry()],
});

router.post('/webhook/line', verifyLineSignature, async (req: Request, res: Response) => {
  const { events } = req.body;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).send('No events');
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      // Accept only message events we care about
      if (ev.type !== 'message' || !ev.message || !ev.source?.userId) continue;

      // Prefer LINE's stable message id as idempotency key
      const lineMessageId = ev.message.id;
      const userId = ev.source.userId;
      const tenantId = 'default'; // derive from DB/channel config as needed
      const now = new Date();
      const punchDate = now.toISOString().split('T')[0];

      // 1) Record delivery event (idempotent)
      const deliveryId = lineMessageId || `${userId}-${ev.timestamp || now.getTime()}-${ev.type}`;
      const logResult = await client.query(
        `INSERT INTO line_delivery_events (line_delivery_id, user_id, event_type, payload, processed_at)
         VALUES ($1, $2, $3, $4, NOW())
         ON CONFLICT (line_delivery_id) DO NOTHING
         RETURNING id`,
        [deliveryId, userId, ev.type, JSON.stringify(ev)]
      );

      // Already processed -> skip business logic (idempotent)
      if (logResult.rowCount === 0) {
        duplicateSuppressed.inc();
        continue;
      }

      // 2) Determine next punch type atomically (within same txn)
      const lastPunch = await client.query(
        `SELECT punch_type FROM punches
         WHERE user_id = $1 AND tenant_id = $2 AND punch_date = $3
         ORDER BY created_at DESC LIMIT 1`,
        [userId, tenantId, punchDate]
      );

      const nextType = lastPunch.rows[0]?.punch_type === 'clock_in' ? 'clock_out' : 'clock_in';

      // 3) Idempotent punch insert
      await client.query(
        `INSERT INTO punches (tenant_id, user_id, punch_type, punch_date, line_message_id, created_at, location)
         VALUES ($1, $2, $3, $4, $5, $6, $7)
         ON CONFLICT (line_message_id) DO NOTHING`,
        [tenantId, userId, nextType, punchDate, lineMessageId, now, req.body.location || null]
      );
    }

    await client.query('COMMIT');
    webhookReceived.inc({ status: 'ok' });
    return res.status(200).send('OK');
  } catch (err) {
    await client.query('ROLLBACK');
    webhookReceived.inc({ status: 'error' });
    console.error('LINE webhook error', { error: err, events });
    return res.status(500).send('ERROR');
  } finally {
    client.release();
  }
});

export default router;
```

Wire into your app:

```ts
// /opt/axentx/workio/server/src/app.ts (or server entry)
import lineWebhookRouter from './routes/line-webhook';
app.use('/webhook/line', lineWebhookRouter);
```

---

### 3) Verification (concrete steps)

1. Start backend:
   ```bash
   cd /opt/axentx/workio/server && npm run dev
   ```

2. Send a webhook with a stable `message.id` and confirm one row:
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -d '{
       "events": [{
         "type": "message",
         "message": { "id": "msg-001", "type": "text", "text": "clock" },
         "source": { "userId": "U123" },
         "timestamp": 1700000000000
       }]
     }'
   ```

3. Repeat same request multiple times; verify only one row exists:
   ```bash
   psql workio -c "SELECT * FROM punches WHERE line_message_id='msg-001';"
   ```

4. Concurrent safety test (run in parallel):
   ```bash
   for i in {1..10}; do
     curl -X POST http://localhost:3000/webhook/line \
       -H "Content-Type: application/json" \
       -d "{\"events\":[{\"type\":\"message\",\"message\":{\"id\":\"msg-concurrent-$i\",\"type\":\"text\"},\"source\":{\"userId\":\"U123\"},\"timestamp\":170000000000$i}]}" &
   done
   wait
   psql workio -c "SELECT line_message_id, punch_type FROM punches WHERE user_id='U123' ORDER BY created_at;"
   ```
   Expect: no constraint violations; each `line_message_id` appears at most once.

5. Check delivery log for audit:
   ```bash
   psql workio -c "SELECT line_delivery_id, event_type, processed_at FROM line_delivery_events;"
   ```

6. (Optional) Observe duplicate suppression in logs/metrics.

---

### 4) Why this resolves contradictions and is actionable

- **Idempotency key choice**: Uses `line_message_id
