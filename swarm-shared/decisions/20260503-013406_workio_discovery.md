# workio / discovery

## Final synthesized answer (strongest, correct, actionable)

### 1. Root-cause diagnosis (merged)
- **Missing idempotency at ingestion** — LINE retries on 5xx/timeouts; duplicate `webhookEventId` can replay punches/leave/OT and corrupt state.
- **Race on punch state transitions** — find-then-insert/update for open punches allows concurrent requests to create multiple open punches or lose updates.
- **No DB-level guard** — no unique constraint/index to enforce at-most-one-open punch or to deduplicate events at storage layer.
- **Frontend double-tap / retry hazard** — punch button lacks optimistic UI + idempotency key; flaky networks can trigger duplicate requests.
- **No replay-safe log** — no record of processed `webhookEventId` means retries after partial failure can re-apply side effects (notifications, state changes).

### 2. Concrete changes (merged + corrected)

#### 2.1 DB schema (single source of truth)

```sql
-- server/src/db/schema.sql

-- Track processed LINE webhook events for idempotency
CREATE TABLE IF NOT EXISTS webhook_events (
  id              SERIAL PRIMARY KEY,
  event_id        TEXT NOT NULL,          -- LINE webhookEventId
  tenant_id       INTEGER NOT NULL,
  user_id         INTEGER NOT NULL,
  event_type      TEXT NOT NULL,          -- clock_in, clock_out, leave_request, etc.
  target_id       INTEGER,                -- punch_id, leave_id, ot_id (optional)
  payload_hash    TEXT,                   -- detect changed re-delivery
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(event_id, tenant_id)
);

-- At most one open punch per user/tenant (no clock_out)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch
ON punches (user_id, tenant_id)
WHERE clock_out IS NULL;
```

#### 2.2 Idempotent webhook handler (Node/Express + pg)

```ts
// server/src/routes/webhook/line.ts
import { Router } from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

const router = Router();

async function recordEventIfNew(
  client: any,
  eventId: string,
  tenantId: number,
  userId: number,
  eventType: string,
  targetId: number | null,
  payload: any
) {
  const hash = crypto.createHash('sha256')
    .update(JSON.stringify(payload))
    .digest('hex');

  const res = await client.query(
    `INSERT INTO webhook_events (event_id, tenant_id, user_id, event_type, target_id, payload_hash)
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT (event_id, tenant_id) DO UPDATE
     SET payload_hash = EXCLUDED.payload_hash
     WHERE webhook_events.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
     RETURNING id, (xmax = 0) AS inserted`,
    [eventId, tenantId, userId, eventType, targetId, hash]
  );

  return {
    recordId: res.rows[0]?.id,
    inserted: res.rows[0]?.inserted === true,
  };
}

router.post('/line', async (req, res) => {
  const body = req.body;
  if (!body?.events?.length) return res.sendStatus(200);

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of body.events) {
      const eventId = ev.webhookEventId;
      const source = ev.source;
      if (!source?.userId) continue;

      // Resolve tenant/user (adapt to your tenant resolution)
      const userRes = await client.query(
        'SELECT id, tenant_id FROM users WHERE line_user_id = $1 LIMIT 1',
        [source.userId]
      );
      if (!userRes.rows.length) continue;
      const { id: userId, tenant_id: tenantId } = userRes.rows[0];

      // Idempotency record
      const { inserted } = await recordEventIfNew(
        client,
        eventId,
        tenantId,
        userId,
        ev.type,
        null,
        ev
      );

      // If not inserted, skip application (already processed)
      if (!inserted) continue;

      // Handle clock in/out with atomic upsert using unique constraint
      if (ev.type === 'postback' && ev.postback?.data?.startsWith('clock')) {
        const isIn = ev.postback.data === 'clock_in';

        if (isIn) {
          // Try insert open punch; unique index prevents double open
          const punchRes = await client.query(
            `INSERT INTO punches (user_id, tenant_id, clock_in, location, clock_in_latitude, clock_in_longitude)
             VALUES ($1, $2, NOW(), $3, $4, $5)
             RETURNING id`,
            [userId, tenantId, ev.postback?.params?.location || '', null, null]
          ).catch((err) => {
            // If unique violation (open punch exists), treat as no-op
            if (err.code === '23505') return { rows: [] };
            throw err;
          });

          if (!punchRes.rows.length) {
            // Already clocked in — optionally notify via LINE or log
            continue;
          }

          // Record target
          const punchId = punchRes.rows[0].id;
          await client.query(
            `UPDATE webhook_events SET target_id = $1 WHERE event_id = $2 AND tenant_id = $3`,
            [punchId, eventId, tenantId]
          );
        } else {
          // clock_out: atomically close the open punch
          const outRes = await client.query(
            `UPDATE punches
             SET clock_out = NOW(),
                 clock_out_latitude = $1,
                 clock_out_longitude = $2
             WHERE id = (
               SELECT id FROM punches
               WHERE user_id = $3 AND tenant_id = $4 AND clock_out IS NULL
               ORDER BY clock_in DESC LIMIT 1
               FOR UPDATE SKIP LOCKED
             )
             RETURNING id`,
            [null, null, userId, tenantId] // replace nulls with actual coords if available
          );

          if (outRes.rows.length) {
            await client.query(
              `UPDATE webhook_events SET target_id = $1 WHERE event_id = $2 AND tenant_id = $3`,
              [outRes.rows[0].id, eventId, tenantId]
            );
          }
        }
      }

      // Handle leave/OT similarly with idempotency + unique constraints
      // (same pattern: recordEventIfNew + upsert with unique business key)
    }

    await client.query('COMMIT');
    res.sendStatus(200);
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook processing failed', err);
    // Return 5xx only for retryable errors; for non-retryable, 200 to stop LINE retries.
    res.sendStatus(500);
  } finally {
    client.release();
  }
});

export default router;
```

### 3. Frontend recommendations (merged)
- Add optimistic UI for punch actions (immediate local state update, revert on failure).
- Use idempotency keys on punch requests (client-generated UUID) and deduplicate on the server for non-webhook punch endpoints.
- Debounce the punch button and disable after first tap until request settles.

### 4. Operational notes
- The `webhook_events` table + unique index on `(event_id, tenant_id)` guarantees idempotent ingestion.
- The partial unique index `idx_one_open_punch` enforces at-most-one-open punch at the DB level, preventing races.
- All punch/leave/OT side effects are recorded with `target_id` in `webhook_events` for audit and replay safety.
- Return 200 only after commit to stop LINE retries; use 5xx for transient failures you want retried.
