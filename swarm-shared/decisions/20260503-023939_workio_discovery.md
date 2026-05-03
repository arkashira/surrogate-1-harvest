# workio / discovery

## 1. Diagnosis
- Missing idempotency key on LINE webhook handler allows duplicate punches when LINE retries (network timeout, 5xx, or slow client).
- Application-level “last punch” check is racy under concurrency: two near-simultaneous webhooks can both read “no recent punch” and insert two clock-ins.
- No DB-level uniqueness guard to reject duplicates (same `employee_id`, `direction`, `date`, and close `timestamp`).
- Punch API returns 200 before transaction is durable; LINE may retry and create duplicates.
- No lightweight idempotency trace for webhook source (LINE `deliveryId`/`timestamp` composite) to short-circuit retries without hitting heavy logic.

## 2. Proposed change
File: `/opt/axentx/workio/server/src/routes/line/webhook.ts`  
Scope:  
- Add idempotency table `line_webhook_deliveries` keyed by `(channel_id, delivery_id)` with TTL.  
- Wrap punch handling in `INSERT ... ON CONFLICT DO NOTHING` and return 200 immediately after commit.  
- Add DB uniqueness constraint on punches: `(employee_id, date, direction, ts)` with small grace window handled at app layer (same second → reject duplicate direction).  
- Short-circuit retries by checking idempotency key before processing.

## 3. Implementation

```bash
# 1) Create idempotency table and punch constraint
psql workio <<'SQL'
CREATE TABLE IF NOT EXISTS line_webhook_deliveries (
  channel_id      TEXT NOT NULL,
  delivery_id     TEXT NOT NULL,
  processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash    TEXT,
  PRIMARY KEY (channel_id, delivery_id)
);
-- Optional: auto-expire after 48h to keep table small
CREATE INDEX IF NOT EXISTS idx_line_webhook_deliveries_processed_at
  ON line_webhook_deliveries (processed_at);

-- Prevent duplicate punches at DB level (same employee+date+direction+exact second)
-- If you want to allow same-second clock-in/out, remove direction from constraint
-- and enforce at app layer instead.
ALTER TABLE punches
  ADD CONSTRAINT uq_punch_employee_date_direction_ts
  UNIQUE (employee_id, date, direction, ts);
SQL
```

```typescript
// 2) /opt/axentx/workio/server/src/routes/line/webhook.ts
import { Router, Request, Response } from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

const router = Router();

function hashPayload(body: any): string {
  return crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
}

router.post('/webhook/line', async (req: Request, res: Response) => {
  const channelId = process.env.LINE_CHANNEL_ID || '';
  const events = req.body.events;
  if (!events || !Array.isArray(events)) return res.sendStatus(400);

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check across all events in this delivery (use first event's deliveryContext if available)
    // LINE provides `deliveryId` in webhook request headers or inside `source`/`deliveryContext` depending on version.
    // We'll derive a deterministic delivery key from header or body hash + timestamp.
    const deliveryId = req.header('X-Line-Delivery') || hashPayload(req.body).slice(0, 24);
    const payloadHash = hashPayload(req.body);

    const idemCheck = await client.query(
      `SELECT 1 FROM line_webhook_deliveries WHERE channel_id = $1 AND delivery_id = $2`,
      [channelId, deliveryId]
    );
    if (idemCheck.rowCount && idemCheck.rowCount > 0) {
      await client.query('ROLLBACK');
      return res.sendStatus(200); // already processed
    }

    // Record delivery before processing to prevent concurrent retries from racing
    await client.query(
      `INSERT INTO line_webhook_deliveries (channel_id, delivery_id, payload_hash) VALUES ($1,$2,$3)`,
      [channelId, deliveryId, payloadHash]
    );

    for (const ev of events) {
      if (ev.type !== 'message' || ev.message?.type !== 'text') continue;
      const userId = ev.source?.userId;
      const text = ev.message.text.trim().toLowerCase();
      if (!userId) continue;

      // Map text to direction (clock-in/clock-out) — adapt to your command scheme
      const direction = text.includes('ออก') || text.includes('out') ? 'out' : 'in';

      // Fetch employee by line_user_id
      const emp = await client.query(
        `SELECT id FROM employees WHERE line_user_id = $1 AND tenant_id IS NOT NULL LIMIT 1`,
        [userId]
      );
      if (!emp.rows[0]) continue;

      const empId = emp.rows[0].id;
      const now = new Date();
      const today = now.toISOString().slice(0, 10);
      const ts = now.toISOString();

      // Try insert; unique constraint prevents duplicates
      try {
        await client.query(
          `INSERT INTO punches (employee_id, tenant_id, direction, ts, date, created_at)
           SELECT $1, tenant_id, $2, $3, $4, NOW() FROM employees WHERE id = $1`,
          [empId, direction, ts, today]
        );
      } catch (err: any) {
        // If unique violation, skip silently (duplicate)
        if (err.code !== '23505') throw err;
      }
    }

    await client.query('COMMIT');
    res.sendStatus(200);
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('LINE webhook error:', err);
    // Return 200 to prevent LINE retry storms on transient errors if desired,
    // or 500 to allow retry for real failures. We recommend 200 only after idempotency
    // record is written; otherwise 500 so LINE retries transient backend failures.
    res.sendStatus(500);
  } finally {
    client.release();
  }
});

export default router;
```

```json
// 3) Ensure middleware is mounted in /opt/axentx/workio/server/src/app.ts (or index.ts)
// app.use('/webhook/line', require('./routes/line/webhook').default);
```

## 4. Verification
1. Apply DB changes: run the `psql` block above and confirm `\d punches` shows the new unique constraint and `line_webhook_deliveries` exists.
2. Start backend: `npm run dev` in `/opt/axentx/workio/server`.
3. Simulate duplicate delivery:
   - Send a valid LINE event payload to `/webhook/line` (use curl/Postman).
   - Immediately replay same payload with same `X-Line-Delivery` header (or same body).
   - Confirm only one punch row is created (`SELECT count(*) FROM punches WHERE employee_id = ? AND ts::text LIKE ?`).
   - Confirm second request returns 200 and does not insert a punch.
4. Concurrent race test:
   - Fire two requests in parallel (same deliveryId) and confirm only one wins (idempotency row blocks second) and punch count remains 1.
5. Check logs: no unique violation errors for duplicates; transient errors (non-23505) are logged and return 500.
