# workio / discovery

Candidate 3:
- **Diagnosis**
  - Idempotency is missing on the LINE webhook endpoint, so retries or duplicate deliveries can create duplicate punches/leave/OT records.
  - Punch state transitions are not race-safe; concurrent webhook deliveries can create multiple open punches per user/tenant.
  - There is no transactional boundary around punch/leave/OT writes, so partial failures can leave inconsistent state.
  - No unique constraint/index exists to enforce “only one open punch per user/tenant,” relying only on app logic.
  - No deduplication store for LINE event IDs, so repeated events with the same delivery/webhook-event ID are processed multiple times.

- **Proposed change**
  - File: `server/src/routes/webhook/line.ts` (or equivalent webhook handler)
  - Scope: add idempotency + transactional upsert for punch events using LINE’s `deliveryId` (or `webhookEventId`) as the idempotency key and a unique partial index to enforce one open punch per user/tenant.

- **Implementation**
  - DB schema (run once):
    ```sql
    -- Idempotency table for LINE webhook events
    CREATE TABLE IF NOT EXISTS line_webhook_events (
      id         TEXT PRIMARY KEY,               -- LINE deliveryId or webhookEventId
      tenant_id  INTEGER NOT NULL,
      user_id    INTEGER NOT NULL,
      event_type TEXT NOT NULL,                  -- 'punch', 'leave', 'ot'
      record_id  INTEGER,                        -- FK to created record
      created_at TIMESTAMPTZ DEFAULT NOW()
    );

    -- Enforce at most one open punch per user/tenant
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch_per_user_tenant
    ON punches (user_id, tenant_id)
    WHERE status IN ('in', 'OPEN') OR status IS NULL;
    ```
  - Webhook handler (transactional + idempotent):
    ```ts
    import { Router } from 'express';
    import { pool } from '../db';
    import { verifyLineSignature } from '../utils/line';

    const router = Router();

    router.post('/line', async (req, res) => {
      const sig = req.headers['x-line-signature'] as string;
      const body = req.body;

      if (!verifyLineSignature(JSON.stringify(body), sig)) {
        return res.status(401).send('Invalid signature');
      }

      const conn = await pool.connect();
      try {
        await conn.query('BEGIN');

        for (const event of body.events) {
          const deliveryId = event.deliveryId || event.webhookEventId;
          if (!deliveryId) continue;

          // Idempotency check
          const exists = await conn.query(
            `SELECT 1 FROM line_webhook_events WHERE id = $1`,
            [deliveryId]
          );
          if (exists.rows.length > 0) continue;

          if (event.type === 'message' && event.message.type === 'text') {
            const text = event.message.text.trim().toLowerCase();
            const userId = event.source.userId;
            const tenantId = 1; // derive from user mapping

            if (text === 'clock in' || text === 'clock out') {
              const isClockIn = text === 'clock in';

              if (isClockIn) {
                const insertPunch = await conn.query(
                  `INSERT INTO punches (user_id, tenant_id, clock_in_at, status)
                   VALUES ($1, $2, NOW(), 'in')
                   RETURNING id`,
                  [userId, tenantId]
                );

                await conn.query(
                  `INSERT INTO line_webhook_events (id, tenant_id, user_id, event_type, record_id)
                   VALUES ($1, $2, $3, 'punch', $4)`,
                  [deliveryId, tenantId, userId, insertPunch.rows[0].id]
                );
              } else {
                const closeRes = await conn.query(
                  `UPDATE punches
                   SET clock_out_at = NOW(), status = 'out'
                   WHERE user_id = $1 AND tenant_id = $2 AND status = 'in'
                   RETURNING id`,
                  [userId, tenantId]
                );

                await conn.query(
                  `INSERT INTO line_webhook_events (id, tenant_id, user_id, event_type, record_id)
                   VALUES ($1, $2, $3, 'punch', $4)`,
                  [deliveryId, tenantId, userId, closeRes.rows[0]?.id || null]
                );
              }
            }
          }
        }

        await conn.query('COMMIT');
        return res.status(200).send('OK');
      } catch (err) {
        await conn.query('ROLLBACK');
        console.error(err);
        return res.status(500).send('Server error');
      } finally {
        conn.release();
      }
    });

    export default router;
    ```

- **Verification**
  - Send duplicate LINE webhook events (same deliveryId) and confirm only one punch record is created/updated.
  - Confirm partial failures roll back (no orphaned idempotency rows or punch rows).
  - Confirm concurrent clock-in attempts fail gracefully (unique partial index prevents double-open punches).
  - Check dashboard/report consistency after retries and replays.

---

Synthesize the best parts of multiple AI proposals. Combine the strongest insights into ONE final answer. Resolve contradictions in favor of correctness + concrete actionability.
