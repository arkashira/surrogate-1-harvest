# workio / discovery

Candidate 2 was cut off. Below is the **synthesized, final answer** that merges the strongest, most correct and actionable parts of both proposals, resolves contradictions, and produces a single, production-ready plan.

---

## 1. Unified Diagnosis (accepted)

- **Missing idempotency**: LINE redelivers webhooks; without an idempotency key, retries create duplicate punch rows.
- **Race conditions**: Non-atomic read-then-insert allows two active punches for the same user.
- **No database enforcement**: No unique constraint prevents multiple active punches (`clocked_out_at IS NULL`).
- **No server-side de-dup**: No table or check for `line_delivery_id` (or similar token) means silent duplication on network retries.
- **Unclear return state**: After upsert, clients can’t reliably know current punch state if retries occur.

---

## 2. Final Design Decisions (resolved contradictions)

- Use **one idempotency table** keyed by `line_delivery_id` (from `X-Line-Delivery-Id` header).  
  - Simpler and sufficient for this webhook than a separate `line_deliveries` table.
  - Provides exactly-once processing and audit trail.
- Enforce **exactly one active punch per user** with a **partial unique index** on `punches(user_id)` where `clocked_out_at IS NULL`.
- Perform **atomic upsert in a single transaction** so concurrent/duplicate deliveries cannot create two active punches.
- Return **stable punch state** after every request (including retries) so clients can trust the response.
- Keep changes minimal and migration-safe: add schema objects if-not-exists; no breaking app rewrites.

---

## 3. Implementation (single final version)

### 3.1 DB schema (run once)

```sql
-- Idempotency table for LINE webhook deliveries
CREATE TABLE IF NOT EXISTS punch_idempotency (
  line_delivery_id TEXT PRIMARY KEY,
  user_id           INTEGER NOT NULL,
  punch_id          INTEGER NOT NULL,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Enforce at most one active punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_active_per_user
ON punches (user_id)
WHERE clocked_out_at IS NULL;
```

### 3.2 Route handler (atomic upsert + idempotency)

File: `/opt/axentx/workio/server/src/routes/punch.ts` (or equivalent)

```ts
import { Router } from 'express';
import { pool } from '../db';

const router = Router();

// Optional: preserve LINE header if behind proxy
export function preserveLineHeaders(req, _res, next) {
  if (req.headers['x-forwarded-line-delivery-id']) {
    req.headers['x-line-delivery-id'] = req.headers['x-forwarded-line-delivery-id'] as string;
  }
  next();
}

router.post('/webhook/line', preserveLineHeaders, async (req, res) => {
  const { events } = req.body;
  const deliveryId = req.headers['x-line-delivery-id'] as string;

  if (!deliveryId) {
    return res.status(400).json({ error: 'missing x-line-delivery-id' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check
    const idem = await client.query(
      `SELECT punch_id FROM punch_idempotency WHERE line_delivery_id = $1`,
      [deliveryId]
    );

    if (idem.rows.length > 0) {
      const punch = await client.query(
        `SELECT id, user_id, clocked_in_at, clocked_out_at, location
         FROM punches WHERE id = $1`,
        [idem.rows[0].punch_id]
      );
      await client.query('COMMIT');
      return res.json({ ok: true, punch: punch.rows[0] });
    }

    // Validate event
    const event = events?.[0];
    if (!event?.source?.userId || event.type !== 'message') {
      await client.query('ROLLBACK');
      return res.status(400).json({ error: 'invalid event' });
    }

    const userId = event.source.userId; // map to internal user via your LINE<->user table
    const now = new Date();

    // Atomic upsert:
    // - If an active punch exists, close it.
    // - Otherwise create a new active punch.
    const upsertRes = await client.query(
      `WITH closed AS (
         UPDATE punches
         SET clocked_out_at = $3, updated_at = NOW()
         WHERE user_id = $1 AND clocked_out_at IS NULL
         RETURNING id
       )
       INSERT INTO punches (user_id, clocked_in_at, clocked_out_at, location)
       SELECT $1, $3, NULL, $2
       WHERE NOT EXISTS (SELECT 1 FROM closed)
       RETURNING id, clocked_in_at, clocked_out_at, location`,
      [userId, event.message?.text || '', now]
    );

    let punch = upsertRes.rows[0];

    // If upsert closed an existing punch and did not return a new row,
    // fetch the latest state (the previously active punch, now closed).
    if (!punch) {
      const latest = await client.query(
        `SELECT id, user_id, clocked_in_at, clocked_out_at, location
         FROM punches WHERE user_id = $1 ORDER BY clocked_in_at DESC LIMIT 1`,
        [userId]
      );
      punch = latest.rows[0];
    }

    // Record idempotency
    await client.query(
      `INSERT INTO punch_idempotency (line_delivery_id, user_id, punch_id)
       VALUES ($1, $2, $3)`,
      [deliveryId, userId, punch.id]
    );

    await client.query('COMMIT');
    res.json({ ok: true, punch });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Punch webhook error', err);
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});

export default router;
```

---

## 4. Verification (single checklist)

1. **Schema applied**
   - Confirm `punch_idempotency` exists with PK on `line_delivery_id`.
   - Confirm partial unique index `idx_punches_one_active_per_user` exists.

2. **Idempotency works**
   - Send same payload twice with identical `X-Line-Delivery-Id`.
   - Expect: second request returns same `punch_id`; no new `punches` row.

3. **Active punch invariant**
   - Clock in, then attempt another clock in before clocking out.
   - Expect: only one row with `clocked_out_at IS NULL` for that user.

4. **Concurrent/duplicate delivery safety**
   - Simulate concurrent POSTs with same `X-Line-Delivery-Id`.
   - Expect: no duplicate active punches; idempotency prevents double-processing.

5. **End-to-end via LINE (manual)**
   - Send clock-in message from test LINE user.
   - Resend captured webhook payload to your endpoint.
   - Verify dashboard shows one punch record and correct state.
