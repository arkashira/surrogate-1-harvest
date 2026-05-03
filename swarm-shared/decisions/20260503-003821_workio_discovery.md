# workio / discovery

## Final synthesized solution

### 1. Diagnosis (merged)
- **Missing idempotency key** on `/webhook/line` allows LINE retries (network blips, 5xx, slow client) to create duplicate punches.
- **No partial unique constraint** enforcing “one open punch per user” (`clock_out_at IS NULL`) allows race/retry corruption.
- **Non-transactional upsert** allows concurrent clock-ins to create multiple open rows.
- **No de-duplication guard** for retries within a short window (same user + event + timestamp).
- **No observability** (metrics/logging) to detect duplicates in production.

### 2. Scope
- `workio/server/src/db/schema.sql` (or migration) — constraints and idempotency column.
- `workio/server/src/routes/webhook/line.js` (or `.ts`) — idempotent, transactional handler.
- Optional: `workio/server/src/db/migrations/` for reproducible deployments.

### 3. Implementation

#### 3.1 Schema changes
```sql
-- workio/server/src/db/schema.sql
-- Add idempotency column and ensure single-open-punch invariant

ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS line_delivery_id VARCHAR(128) NULL,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Idempotency guard: one row per delivery id (only when present)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_delivery_id
  ON punches (line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;

-- Business invariant: at most one open punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user
  ON punches (user_id)
  WHERE clock_out_at IS NULL;
```

#### 3.2 Webhook handler (idempotent + transactional)
```js
// workio/server/src/routes/webhook/line.js
const express = require('express');
const router = express.Router();
const db = require('../../db');

function normalizeAction(text = '') {
  const t = text.toLowerCase().trim();
  if (t === 'clock_in' || t === 'เข้างาน') return 'clock_in';
  if (t === 'clock_out' || t === 'เลิกงาน') return 'clock_out';
  return null;
}

router.post('/line', async (req, res) => {
  const events = req.body.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).json({ error: 'invalid payload' });
  }

  const client = await db.pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      const userId = ev?.source?.userId;
      const timestamp = ev?.timestamp;
      const deliveryId = userId && timestamp ? `${userId}:${timestamp}:${ev.type}` : null;
      const action = normalizeAction(ev?.message?.text);

      // Skip non-punch events
      if (!action) continue;

      // Idempotency check (short-circuit retries)
      if (deliveryId) {
        const dup = await client.query(
          `SELECT id FROM punches WHERE line_delivery_id = $1`,
          [deliveryId]
        );
        if (dup.rows.length > 0) continue;
      }

      // Lock any open punch for this user to serialize concurrent attempts
      const openPunch = await client.query(
        `SELECT id FROM punches WHERE user_id = $1 AND clock_out_at IS NULL FOR UPDATE`,
        [userId]
      );

      if (action === 'clock_in') {
        // Auto-close any dangling open punch to preserve invariant
        if (openPunch.rows.length > 0) {
          await client.query(
            `UPDATE punches SET clock_out_at = NOW(), updated_at = NOW() WHERE id = $1`,
            [openPunch.rows[0].id]
          );
        }

        await client.query(
          `INSERT INTO punches (user_id, clock_in_at, line_delivery_id, created_at, updated_at)
           VALUES ($1, NOW(), $2, NOW(), NOW())`,
          [userId, deliveryId]
        );
      } else if (action === 'clock_out') {
        if (openPunch.rows.length === 0) {
          // Graceful: create a closed punch immediately
          await client.query(
            `INSERT INTO punches (user_id, clock_in_at, clock_out_at, line_delivery_id, created_at, updated_at)
             VALUES ($1, NOW(), NOW(), $2, NOW(), NOW())`,
            [userId, deliveryId]
          );
        } else {
          await client.query(
            `UPDATE punches SET clock_out_at = NOW(), updated_at = NOW() WHERE id = $1`,
            [openPunch.rows[0].id]
          );
        }
      }
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook error:', {
      message: err.message,
      code: err.code,
      detail: err.detail
    });

    // Unique violation on line_delivery_id => idempotent success
    if (err.code === '23505') {
      return res.status(200).json({ ok: true });
    }

    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});

module.exports = router;
```

### 4. Verification (merged + concrete)

1. **Schema check**
   ```bash
   psql workio -c "\d punches"
   # Confirm line_delivery_id and indexes idx_punches_line_delivery_id and idx_punches_one_open_per_user exist
   ```

2. **Idempotency test (LINE retry)**
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H 'Content-Type: application/json' \
     -d '{"events":[{"source":{"userId":"U123"},"timestamp":1712345678901,"type":"message","message":{"text":"clock_in"}}]}'
   ```
   - Run same request twice; verify only one punch row with matching `line_delivery_id`.

3. **Concurrent race test**
   - Fire two parallel `clock_in` requests for same user (different deliveryIds or same) and confirm only one open punch exists (partial unique index prevents two `clock_out_at IS NULL` rows).

4. **Auto-close behavior**
   - Clock in twice without clocking out; confirm first punch is auto-closed and second creates a new open punch (or only one open punch remains).

5. **Graceful clock-out**
   - Clock out with no open punch; confirm a closed punch row is created (`clock_in_at` ≈ `clock_out_at`).

6. **Observability**
   - Check logs for `LINE webhook error` entries and monitor for `23505` (idempotent duplicates) vs other errors.

7. **Return behavior**
   - Ensure endpoint returns `200` for duplicates and valid requests so LINE stops retrying.
