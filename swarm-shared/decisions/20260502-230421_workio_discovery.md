# workio / discovery

## Final Synthesis — Correct, Actionable Fix

**Diagnosis (merged, prioritized)**
- Root cause is **non-idempotent write path**: duplicate LINE webhook deliveries, client retries, and concurrent requests can create multiple punches for the same `(employee_id, punch_type, date)`.
- Application-level checks are insufficient under concurrency or retries (read-then-write race).
- No DB-level uniqueness guard allows duplicates to persist.
- No idempotency mechanism for retries (client or webhook) and no lightweight dedupe for LINE webhook event IDs.

**Chosen approach**
- Enforce uniqueness at the **database layer** (single source of truth).
- Add **idempotency for client retries** via `Idempotency-Key` and a small mapping table.
- Add **dedupe for LINE webhooks** via a `webhook_events` table keyed by LINE’s event/delivery ID.
- Make punch creation **atomic** (`INSERT … ON CONFLICT DO NOTHING`) inside a transaction.
- Keep schema migration safe and verifiable.

---

### 1) Schema changes (`workio/server/src/db/schema.sql`)

```sql
-- 1) Prevent duplicate punches per employee per day per punch_type
-- Assumes table name: punches; columns: punch_id, employee_id, punch_type, punch_time, created_at
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_employee_day_type
  ON punches (employee_id, DATE(punch_time), punch_type)
  WHERE punch_type IN ('in', 'out');

-- 2) Idempotency table for client retries (lightweight)
CREATE TABLE IF NOT EXISTS punch_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  punch_id        INTEGER NOT NULL REFERENCES punches(punch_id) ON DELETE CASCADE,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_punch_idempotency_key
  ON punch_idempotency (idempotency_key);

-- 3) Dedupe table for LINE webhook deliveries (prevent redelivery duplicates)
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id        TEXT PRIMARY KEY,   -- e.g., LINE webhook event.id or a stable delivery id
  handler         TEXT NOT NULL,      -- e.g., 'punch'
  target_id       INTEGER NOT NULL,   -- e.g., punch_id or employee_id (flexible)
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_handler
  ON webhook_events (handler, event_id);
```

---

### 2) Route handler (`workio/server/src/routes/punch.ts`)

```ts
import { Router, Request, Response } from 'express';
import { pool } from '../db';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

/**
 * POST /punch
 * Body: { employee_id: number, punch_type: 'in' | 'out' }
 * Header: Idempotency-Key (optional)
 */
router.post('/', async (req: Request, res: Response) => {
  const { employee_id, punch_type } = req.body;
  const idempotencyKey = req.header('Idempotency-Key') || uuidv4();
  const client = await pool.connect();

  if (!employee_id || !['in', 'out'].includes(punch_type)) {
    client.release();
    return res.status(400).json({ error: 'Invalid payload' });
  }

  try {
    await client.query('BEGIN');

    // Idempotency check: if key exists, return existing punch
    const idem = await client.query(
      'SELECT punch_id FROM punch_idempotency WHERE idempotency_key = $1',
      [idempotencyKey]
    );
    if (idem.rows.length > 0) {
      const punch = await client.query('SELECT * FROM punches WHERE punch_id = $1', [
        idem.rows[0].punch_id,
      ]);
      await client.query('COMMIT');
      client.release();
      return res.json({ punch: punch.rows[0], idempotent: true });
    }

    // Try insert; unique index prevents duplicates at DB level
    const insertRes = await client.query(
      `INSERT INTO punches (employee_id, punch_type, punch_time)
       VALUES ($1, $2, NOW())
       ON CONFLICT (employee_id, DATE(punch_time), punch_type) DO NOTHING
       RETURNING *`,
      [employee_id, punch_type]
    );

    let punch = insertRes.rows[0];

    // If conflict and no row returned, fetch existing punch for this employee/type/day
    if (!punch) {
      const existing = await client.query(
        `SELECT * FROM punches
         WHERE employee_id = $1
           AND punch_type = $2
           AND DATE(punch_time) = DATE(NOW())
         ORDER BY punch_time DESC
         LIMIT 1`,
        [employee_id, punch_type]
      );
      punch = existing.rows[0];
    }

    // Record idempotency mapping
    await client.query(
      'INSERT INTO punch_idempotency (idempotency_key, punch_id) VALUES ($1, $2)',
      [idempotencyKey, punch.punch_id]
    );

    await client.query('COMMIT');
    client.release();
    return res.json({ punch, idempotent: false });
  } catch (err) {
    await client.query('ROLLBACK');
    client.release();
    console.error('Punch error:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
```

---

### 3) LINE webhook handler (dedupe example)

Wherever LINE webhooks are processed (e.g., before calling punch logic):

```ts
async function handleLineWebhook(event: any, handler: string, targetId: number) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const exists = await client.query(
      'SELECT 1 FROM webhook_events WHERE event_id = $1 AND handler = $2',
      [event.id, handler]
    );
    if (exists.rows.length > 0) {
      await client.query('COMMIT');
      client.release();
      return { already_processed: true };
    }

    // Process (e.g., call punch creation) then record event
    // ... your punch creation logic here ...

    await client.query(
      'INSERT INTO webhook_events (event_id, handler, target_id) VALUES ($1, $2, $3)',
      [event.id, handler, targetId]
    );
    await client.query('COMMIT');
    client.release();
    return { processed: true };
  } catch (err) {
    await client.query('ROLLBACK');
    client.release();
    throw err;
  }
}
```

---

### 4) Migration & deployment steps

```bash
cd /opt/axentx/workio

# 1) Backup schema
cp server/src/db/schema.sql server/src/db/schema.sql.bak

# 2) Append the new statements to schema.sql (or run via migration tool)
#    (The CREATE statements above should be applied to the DB.)

# 3) Apply to database (example using psql)
psql $DATABASE_URL -f server/src/db/schema.sql

# 4) Verify objects exist
psql $DATABASE_URL -c "\d punches"
psql $DATABASE_URL -c "\d punch_idempotency"
psql $DATABASE_URL -c "\d webhook_events"

# 5) Restart backend
cd server && npm run dev
```

---

### 5) Verification checklist

1. **Schema** — confirm unique index and tables exist (see above).
2. **Duplicate prevention** — same `Idempotency-Key` returns same punch; second request does not create a row.
3. **Race condition** — concurrent requests without idempotency key still produce only one punch per `(employee_id, punch_type, date)` (enforced by unique index).
4. **LINE redelivery** — repeated webhook with same `event.id` is rejected by `webhook_events` dedupe.
5. **Logs** — no unique constraint violation errors during concurrent or retry scenarios.
