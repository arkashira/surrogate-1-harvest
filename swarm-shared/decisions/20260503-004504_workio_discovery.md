# workio / discovery

## Final consolidated implementation

**File changed**: `workio/server/src/routes/webhook/line.ts`  
**DB changes**: one partial unique constraint + lightweight idempotency table + optional cleanup job.

### 1. DB migration (run once)
```sql
-- 1) One open punch per user
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_punches_one_open_per_user
  ON punches (user_id)
  WHERE clock_out_at IS NULL;

-- 2) Idempotency table for LINE webhooks (short TTL)
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_id        TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  action          TEXT NOT NULL,   -- 'clock_in' | 'clock_out'
  punch_id        BIGINT,          -- FK to punches (nullable for clock-in races)
  processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_line_webhook_idempotency_processed_at
  ON line_webhook_idempotency (processed_at);

-- 3) Optional: keep punch.idempotency_key for cross-reference (nullable)
ALTER TABLE punches
ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
```

### 2. Idempotent route handler
```ts
// workio/server/src/routes/webhook/line.ts
import express from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

const router = express.Router();

function idempotencyKey(event: any): string {
  if (event?.id) return `line:${event.id}`;
  return `line:hash:${crypto.createHash('sha256').update(JSON.stringify(event)).digest('hex')}`;
}

router.post('/', async (req, res) => {
  const payload = req.body;
  const events = payload.events || [];

  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).json({ error: 'No events' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const event of events) {
      if (event?.type !== 'message' || event.message?.type !== 'text') continue;

      const text = String(event.message.text || '').trim().toLowerCase();
      const userId = event.source?.userId;
      if (!userId || (!text.includes('clock in') && !text.includes('clock out'))) continue;

      const isClockIn = text.includes('clock in');
      const key = idempotencyKey(event);

      // Idempotency check
      const dup = await client.query(
        `SELECT idempotency_key FROM line_webhook_idempotency WHERE idempotency_key = $1`,
        [key]
      );
      if (dup.rows.length > 0) {
        // Already processed; skip but continue processing other events
        continue;
      }

      // Record idempotency first (prevents races)
      await client.query(
        `INSERT INTO line_webhook_idempotency (idempotency_key, event_id, user_id, action)
         VALUES ($1, $2, $3, $4)`,
        [key, event.id || '', userId, isClockIn ? 'clock_in' : 'clock_out']
      );

      if (isClockIn) {
        // Atomic insert-if-no-open-punch; unique constraint prevents double open
        try {
          const result = await client.query(
            `INSERT INTO punches (user_id, clock_in_at, idempotency_key)
             VALUES ($1, NOW(), $2)
             RETURNING id`,
            [userId, key]
          );
          // Update idempotency row with punch id
          await client.query(
            `UPDATE line_webhook_idempotency SET punch_id = $1 WHERE idempotency_key = $2`,
            [result.rows[0].id, key]
          );
        } catch (err: any) {
          // Unique violation on idx_punches_one_open_per_user -> already clocked in
          if (err.code === '23505') {
            // Keep idempotency row; treat as no-op
            continue;
          }
          throw err;
        }
      } else {
        // Clock out: close open punch
        const result = await client.query(
          `UPDATE punches
           SET clock_out_at = NOW(), idempotency_key = $1
           WHERE user_id = $2 AND clock_out_at IS NULL
           RETURNING id`,
          [key, userId]
        );
        // If no open punch, still record idempotency (idempotent no-op)
        if (result.rows.length > 0) {
          await client.query(
            `UPDATE line_webhook_idempotency SET punch_id = $1 WHERE idempotency_key = $2`,
            [result.rows[0].id, key]
          );
        }
      }
    }

    await client.query('COMMIT');
    res.status(200).json({ status: 'ok' });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook error', err);
    // 5xx allows LINE retry; idempotency prevents duplicates
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});

export default router;
```

### 3. Optional cleanup job (keeps idempotency table small)
Run daily via cron/pg_cron:
```sql
DELETE FROM line_webhook_idempotency
WHERE processed_at < NOW() - INTERVAL '7 days';
```

### 4. Verification checklist
- **Duplicate POST test**: replay same event id; confirm only one punch row created and no second open punch.
- **Race test**: concurrent clock-in requests for same user; verify only one succeeds (others hit unique constraint and are ignored).
- **Constraint check**:
  ```sql
  SELECT user_id, count(*) FROM punches
  WHERE clock_out_at IS NULL GROUP BY user_id HAVING count(*) > 1;
  ```
  Must return zero rows.
- **Idempotency table**: confirm entries exist for processed events and are reused on retries.
- **Logging**: add structured logs for duplicate detection and constraint violations to aid debugging.
