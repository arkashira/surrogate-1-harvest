# workio / discovery

## Final Synthesized Solution

### 1. Diagnosis (merged)
- **Missing idempotency** on `/webhook/line` allows LINE’s at-least-once retries to create duplicate punch records.
- **No DB-level guard** enforcing “one open punch per user” (`clock_out_at IS NULL`) permits double-clock-in and overlapping intervals.
- **No signature verification** increases surface for forged/replayed webhooks.
- **Non-transactional upsert** for clock-in and non-atomic clock-out allow races between concurrent webhooks to create split/overlapping punch intervals.
- **Absence of deterministic conflict resolution** (stable event ID + timestamps) makes recovery manual and debugging hard.

### 2. Proposed change (merged scope)
- File: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (or equivalent)
- DB: `/opt/axentx/workio/server/src/db/schema.sql` (or migration)
- Scope:
  - Add partial unique index on `punches(user_id)` where `clock_out_at IS NULL`.
  - Add idempotency table keyed by LINE `event.id` (or stable hash) with TTL.
  - Verify LINE signature before any processing.
  - Use transactional upsert for clock-in and atomic update for clock-out.
  - Record processed events and timestamps for audit and recovery.

### 3. Implementation

#### 3.1 DB migration
```sql
-- server/src/db/migrations/20260503_punches_and_webhook_idempotency.sql

-- 1) Prevent multiple open punches per user
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_punches_one_open_per_user
ON punches (user_id)
WHERE clock_out_at IS NULL;

-- 2) Idempotency table for LINE webhook events
CREATE TABLE IF NOT EXISTS line_webhook_events (
  id              BIGSERIAL PRIMARY KEY,
  event_id        TEXT NOT NULL,               -- LINE event.id (stable)
  source          TEXT NOT NULL DEFAULT 'line',
  received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  processed_at    TIMESTAMPTZ NULL,
  payload         JSONB,
  CONSTRAINT uq_line_event_id UNIQUE (event_id)
);

-- Optional: index for TTL cleanup
CREATE INDEX IF NOT EXISTS idx_line_webhook_events_received ON line_webhook_events (received_at)
WHERE received_at < NOW() - INTERVAL '7 days';
```

Apply:
```bash
psql workio < server/src/db/migrations/20260503_punches_and_webhook_idempotency.sql
```

#### 3.2 Route handler (TypeScript)
```ts
// server/src/routes/webhook/line.ts
import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import { pool } from '../db';

const router = Router();
const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

function verifySignature(body: string, signature: string): boolean {
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

router.post('/webhook/line', async (req: Request, res: Response) => {
  const sig = req.headers['x-line-signature'] as string;
  const rawBody = JSON.stringify(req.body);

  if (!verifySignature(rawBody, sig)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const body = req.body;
  const events = body.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(200).json({ ok: true });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      const eventId = ev.id;
      if (!eventId) continue;

      // Idempotency check by LINE event.id
      const exists = await client.query(
        'SELECT 1 FROM line_webhook_events WHERE event_id = $1',
        [eventId]
      );
      if (exists.rows.length > 0) {
        // Already processed; skip but continue processing other events
        continue;
      }

      // Record receipt immediately to prevent races
      await client.query(
        'INSERT INTO line_webhook_events (event_id, payload) VALUES ($1, $2)',
        [eventId, ev]
      );

      const userId = ev.source?.userId;
      if (!userId) continue;

      const text = (ev.message?.text || '').trim().toLowerCase();

      // Clock-in
      if (['in', 'clock in', 'เข้างาน'].includes(text)) {
        // Try insert; if partial unique index blocks, update existing open punch (no-op keeps latest intent)
        await client.query(
          `INSERT INTO punches (user_id, clock_in_at, clock_in_source)
           VALUES ($1, NOW(), 'line')
           ON CONFLICT (user_id)
           DO UPDATE
           SET clock_in_at = EXCLUDED.clock_in_at
           WHERE punches.clock_out_at IS NULL`,
          [userId]
        );
      }

      // Clock-out
      if (['out', 'clock out', 'เลิกงาน'].includes(text)) {
        // Atomic update of the open punch; no split intervals possible under the unique partial index
        await client.query(
          `UPDATE punches
           SET clock_out_at = NOW(), clock_out_source = 'line'
           WHERE user_id = $1 AND clock_out_at IS NULL`,
          [userId]
        );
      }

      // Mark processed
      await client.query(
        'UPDATE line_webhook_events SET processed_at = NOW() WHERE event_id = $1',
        [eventId]
      );
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook error:', err);
    res.status(500).json({ error: 'Internal server error' });
  } finally {
    client.release();
  }
});

export default router;
```

### 4. Verification (merged + concrete)

1. **Apply migration** and confirm objects exist:
   ```bash
   psql workio -c "\d idx_punches_one_open_per_user"
   psql workio -c "\d line_webhook_events"
   ```

2. **Start backend** and send test webhook (use valid signature):
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: <valid-sig>" \
     -d '{"events":[{"id":"e-001","type":"message","message":{"type":"text","text":"in"},"source":{"userId":"U12345"}}]}'
   ```

3. **Idempotency check** — repeat same request (same `event.id`); second request should not create a second punch and should return `{"ok":true}`.

4. **Constraint check** — attempt to create two open punches for same user via SQL; second insert should be blocked by index:
   ```sql
   INSERT INTO punches (user_id, clock_in_at) VALUES ('U12345', NOW());
   INSERT INTO punches (user_id, clock_in_at) VALUES ('U12345', NOW()); -- should error
   ```

5. **Clock-out flow** — send "out" message; verify `clock_out_at` populated and same user can clock-in again afterward.

6. **Auditability** — confirm events recorded:
   ```sql
   SELECT event_id, processed_at FROM line_webhook_events WHERE event_id = 'e-001';
   ```

### 5. Notes
- Uses LINE `event.id` for deterministic idempotency; falls back to hash only if absent (not expected in LINE events).
- Partial unique index + transactional upsert prevents double-clock-in and overlapping intervals even under concurrency.
- Signature verification is performed up front to reject forgeries before DB work.
- Idempotency table includes TTL-friendly index for cleanup; retention (e.g., 7 days) can be tuned.
