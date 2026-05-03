# workio / discovery

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **Missing database-level guardrails**: no partial unique index to enforce at most one open punch per user per calendar day (`clock_out_at IS NULL`), allowing duplicates under LINE webhook retries, client double-taps, or concurrent requests.
- **Race-prone app-layer logic**: `find-then-insert/update` allows two concurrent requests to both see no open punch and both insert.
- **No idempotency**: LINE webhook handler lacks an idempotency key, so at-least-once delivery and network retries create extra punches.
- **Stale open punches**: clock-in does not auto-close previous-day open punches, blocking valid new punches and corrupting reports.
- **Missing auditability**: no lightweight audit trail (created_at, updated_at, source, idempotency key) to debug duplicates.

### 2. Schema Changes (single source of truth)
Apply to `/opt/axentx/workio/server/src/db/schema.sql` and run once.

```sql
-- Add audit and idempotency columns (safe, non-destructive)
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS idempotency_key UUID DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'unknown';

-- Idempotency index (only on non-null keys)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency_key
  ON punches (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- One open punch per user per calendar day (authoritative)
-- Uses DATE(clock_in_at) so each day is independent; NULL clock_out marks open.
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user_per_day
  ON punches (user_id, DATE(clock_in_at))
  WHERE clock_out_at IS NULL;

-- Optional: keep updated_at fresh
CREATE OR REPLACE FUNCTION fn_punches_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_punches_updated_at ON punches;
CREATE TRIGGER tg_punches_updated_at
  BEFORE UPDATE ON punches
  FOR EACH ROW EXECUTE FUNCTION fn_punches_updated_at();
```

Apply:

```bash
cd /opt/axentx/workio
sudo -u postgres psql workio -f server/src/db/schema.sql
```

Verify:

```bash
psql workio -c "\d punches"
psql workio -c "\di idx_punches_one_open_per_user_per_day"
psql workio -c "\di idx_punches_idempotency_key"
```

### 3. Idempotent LINE Webhook Handler
Replace or patch `/opt/axentx/workio/server/src/routes/line.ts`.

Key behaviors:
- Use a deterministic idempotency key from LINE (`deliveryContext.id` + `timestamp`) or synthesize one.
- Use a single transaction with `BEGIN` / `COMMIT` / `ROLLBACK`.
- Check idempotency first; if seen, skip processing.
- On clock-in: attempt insert; if partial-index conflict (open punch exists), auto-close stale open punches from earlier days and allow a fresh clock-in. Do not auto-close same-day open punches (user must clock-out).
- On clock-out: close today’s open punch if present; do not create a punch if none exists.
- Record `source = 'line'` and timestamps for audit.

```ts
// /opt/axentx/workio/server/src/routes/line.ts
import { Request, Response } from 'express';
import { pool } from '../db';

export async function handleLineWebhook(req: Request, res: Response) {
  const events = req.body.events || [];

  for (const ev of events) {
    if (ev.type !== 'message' || ev.message.type !== 'text') continue;

    const userId = ev.source.userId;
    const text = (ev.message.text || '').trim().toLowerCase();
    const ts = ev.timestamp || Date.now();
    const deliveryId = ev.deliveryContext?.id;
    const idempotencyKey = deliveryId
      ? `line-delivery:${deliveryId}:${ts}`
      : `line-event:${ts}:${userId}:${text}`;

    const isClockIn = /^(เข้า|in|clock.in)/i.test(text);
    const isClockOut = /^(ออก|out|clock.out)/i.test(text);
    if (!isClockIn && !isClockOut) continue;

    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // Idempotency check
      const idem = await client.query(
        `SELECT id FROM punches WHERE idempotency_key = $1`,
        [idempotencyKey]
      );
      if (idem.rows.length > 0) {
        await client.query('COMMIT');
        continue;
      }

      if (isClockIn) {
        // Try to insert new open punch
        const insertRes = await client.query(
          `INSERT INTO punches (user_id, clock_in_at, idempotency_key, source)
           VALUES ($1, NOW(), $2, 'line')
           ON CONFLICT DO NOTHING
           RETURNING id`,
          [userId, idempotencyKey]
        );

        if (insertRes.rowCount === 0) {
          // Conflict: an open punch exists for today (partial index blocked insert)
          // Optionally ensure stale (previous-day) open punches are closed
          await client.query(
            `UPDATE punches
             SET clock_out_at = clock_in_at + interval '8 hours', -- sensible default if missing
                 idempotency_key = COALESCE(idempotency_key, $1),
                 source = 'line'
             WHERE user_id = $2
               AND clock_out_at IS NULL
               AND DATE(clock_in_at) < DATE(NOW())`,
            [idempotencyKey, userId]
          );

          // Re-check today after cleanup; if still open, do not create second punch
          const todayCheck = await client.query(
            `SELECT id FROM punches
             WHERE user_id = $1
               AND clock_out_at IS NULL
               AND DATE(clock_in_at) = DATE(NOW())`,
            [userId]
          );
          if (todayCheck.rows.length > 0) {
            // Still an open punch today — do nothing (user should clock-out)
            await client.query('COMMIT');
            continue;
          } else {
            // Stale-only case resolved; insert fresh open punch
            await client.query(
              `INSERT INTO punches (user_id, clock_in_at, idempotency_key, source)
               VALUES ($1, NOW(), $2, 'line')`,
              [userId, idempotencyKey]
            );
          }
        }
      } else if (isClockOut) {
        // Close today's open punch if present
        const closeRes = await client.query(
          `UPDATE punches
           SET clock_out_at = NOW(),
               idempotency_key = COALESCE(idempotency_key, $1),
               source = 'line'
           WHERE user_id = $2
             AND clock_out_at IS NULL
             AND DATE(clock_in_at) = DATE(NOW())
           RETURNING id`,
          [idempotencyKey, userId]
        );

        if (closeRes.rowCount === 0) {
          // No open punch today — do not auto-create one; log for visibility
          console.warn(`No open punch to clock-out: userId=${userId}`);
        }
      }

      await client.query('COMMIT');
    } catch (err) {
      await client.query('ROLLBACK');
      console.error('LINE webhook transaction error', err);
    } finally {
      client.release();
    }
  }

  res.status(200).json({ ok: true });
}
```

### 4. Verification (single checklist)

1. **Schema**
   ```bash
   psql workio -c "\d punches"
   psql workio -c "\di idx_punches_one_open_per_user_per_day"
   psql workio -c "\di idx_punches_idempotency_key"
   ```

2. **Race test (concurrent clock-ins)**
   ```bash
   for i in {1..10}; do
     curl -s -X POST http://localhost:3000/webhook/line \
       -H "Content-Type: application
