# workio / discovery

## Final Synthesis — One Correct, Actionable Plan

**Core diagnosis (merged, de-duplicated):**
- `/webhook/line` lacks idempotency handling → LINE retries (network, 5xx, slow client) create duplicate punches.
- No DB-level “one open punch per user” guard (`clock_out_at IS NULL`) → race conditions allow concurrent clock-ins.
- No deterministic de-duping (event ID / idempotency key) and no audit column to retroactively detect/clean duplicates.
- Handler uses read-then-insert without locking → concurrent requests for the same user can both create open punches.

**Chosen approach (correct + actionable):**
- Use **idempotency keys stored in a dedicated table** (not only a `line_event_id` column) because:
  - Idempotency keys can be constructed from LINE event fields that are stable across retries (`userId`, `timestamp`, `messageId/postbackData`).
  - A separate table cleanly supports retries, replays, and cleanup and avoids conflating source metadata with punch state.
- Add a **partial unique index** to enforce “one open punch per user” at the DB level.
- Use **`SELECT … FOR UPDATE` inside a transaction** to lock any open punch row while deciding insert vs update, preventing races.
- Return 5xx on transient failures so LINE retries; idempotency prevents duplicates on retry.

---

## 1. DB schema changes (`schema.sql`)

```sql
-- Idempotency table for LINE webhook events
CREATE TABLE IF NOT EXISTS webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_type      TEXT NOT NULL,
  user_id         INTEGER NOT NULL,
  punch_id        INTEGER NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- One open punch per user (partial unique constraint)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user
ON punches (user_id)
WHERE clock_out_at IS NULL;

-- Optional: index for cleanup
CREATE INDEX IF NOT EXISTS idx_idempotency_created
ON webhook_idempotency (created_at);
```

---

## 2. Webhook handler (`routes/line.ts`)

```ts
import { Router } from 'express';
import { db } from '../db/index.js';

const router = Router();

async function upsertPunchWithIdempotency(
  userId: number,
  eventType: 'clock_in' | 'clock_out',
  idempotencyKey: string,
  location?: { lat: number; lng: number } | null
) {
  const client = await db.connect();
  try {
    await client.query('BEGIN');

    // 1) Fast path: already processed
    const idem = await client.query(
      `SELECT punch_id FROM webhook_idempotency WHERE idempotency_key = $1`,
      [idempotencyKey]
    );
    if (idem.rowCount > 0) {
      await client.query('ROLLBACK');
      return { punchId: idem.rows[0].punch_id, alreadyProcessed: true };
    }

    // 2) Lock any open punch for this user to prevent races
    const open = await client.query(
      `SELECT id FROM punches WHERE user_id = $1 AND clock_out_at IS NULL FOR UPDATE`,
      [userId]
    );

    let punchId: number;

    if (eventType === 'clock_in') {
      if (open.rowCount > 0) {
        // Already clocked in — treat as no-op (or could auto-clock-out previous)
        punchId = open.rows[0].id;
      } else {
        const ins = await client.query(
          `INSERT INTO punches (user_id, clock_in_at, clock_in_location)
           VALUES ($1, NOW(), $2) RETURNING id`,
          [userId, location ? JSON.stringify(location) : null]
        );
        punchId = ins.rows[0].id;
      }
    } else {
      // clock_out
      if (open.rowCount === 0) {
        await client.query('ROLLBACK');
        throw new Error('No open punch to clock out');
      }
      punchId = open.rows[0].id;
      await client.query(
        `UPDATE punches SET clock_out_at = NOW(), clock_out_location = $1 WHERE id = $2`,
        [location ? JSON.stringify(location) : null, punchId]
      );
    }

    // 3) Record idempotency
    await client.query(
      `INSERT INTO webhook_idempotency (idempotency_key, event_type, user_id, punch_id)
       VALUES ($1, $2, $3, $4)`,
      [idempotencyKey, eventType, userId, punchId]
    );

    await client.query('COMMIT');
    return { punchId, alreadyProcessed: false };
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}

router.post('/webhook/line', async (req, res) => {
  try {
    const events = req.body.events || [];
    for (const ev of events) {
      if (ev.type !== 'message' && ev.type !== 'postback') continue;

      // Stable idempotency key from LINE event (same across retries)
      const idempotencyKey = `line:${ev.source.userId}:${ev.timestamp}:${ev.type}:${ev.message?.id || ev.postback?.data || 'default'}`;

      const action = ev.postback?.data || ev.message?.text || '';
      const eventType = action.includes('out') ? 'clock_out' : 'clock_in';

      const userRow = await db.query(
        `SELECT id FROM users WHERE line_user_id = $1`,
        [ev.source.userId]
      );
      if (userRow.rowCount === 0) continue;

      const userId = userRow.rows[0].id;
      const location = null; // optionally parse location from ev if available

      await upsertPunchWithIdempotency(userId, eventType, idempotencyKey, location);
    }

    res.status(200).json({ ok: true });
  } catch (err) {
    console.error('LINE webhook error', err);
    // 5xx allows LINE to retry; idempotency prevents duplicates
    res.status(500).json({ error: 'internal' });
  }
});

export default router;
```

---

## 3. Cleanup job (optional, once daily)

```sql
-- Retain idempotency keys for 7 days
DELETE FROM webhook_idempotency WHERE created_at < NOW() - INTERVAL '7 days';
```

---

## 4. Verification (minimal, high-value)

1. **Schema check**
   ```bash
   psql workio -c "\d punches"
   psql workio -c "\d webhook_idempotency"
   ```
   Confirm partial index `idx_punches_one_open_per_user` and idempotency table exist.

2. **Duplicate prevention**
   - Simulate two concurrent POSTs with the same `idempotencyKey`.
   - Verify only one punch row is created; second request returns existing `punch_id`.

3. **Open-punch constraint**
   - Manually insert an open punch for a user.
   - Attempt to insert another open punch for the same user (direct SQL or handler).
   - Confirm second insert fails with unique violation.

4. **Idempotency key uniqueness**
   - Send the same LINE event twice.
   - Confirm `webhook_idempotency` has one row and punch count unchanged on second request.

5. **End-to-end via LINE (if credentials available)**
   - Clock in via LINE, retry same message (or simulate network retry).
   - Verify dashboard shows one clock-in and later one clock-out, no duplicate rows.
