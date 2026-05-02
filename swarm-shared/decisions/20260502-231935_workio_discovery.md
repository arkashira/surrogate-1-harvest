# workio / discovery

## Final Synthesized Solution

### Diagnosis (merged)
- LINE webhook redeliveries and concurrent deliveries create duplicate punch rows because there is no idempotency guard at the storage or application layer.
- Punch creation uses non-atomic read-then-insert, allowing races under retries/concurrency.
- No DB-level uniqueness to enforce “one active clock-in per employee per day.”
- Missing state-machine enforcement for clock-in/clock-out (double clock-in without clock-out).
- No lightweight idempotency record or audit field to reconcile duplicates after the fact.

### Schema changes (`workio/server/src/db/schema.sql`)
Apply these idempotent migrations:

```sql
-- Idempotency table for LINE webhook retries (lightweight, TTL-cleanable)
CREATE TABLE IF NOT EXISTS webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_type      TEXT NOT NULL,
  employee_id     INTEGER NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Optional: index for cleanup
CREATE INDEX IF NOT EXISTS idx_webhook_idempotency_created
  ON webhook_idempotency(created_at);

-- Add audit/idempotency column to punches for traceability
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS source_event_id TEXT;

-- Enforce one active clock-in per employee per day.
-- Allow multiple clock-outs (do not include punch_type='out' or active=false in this constraint).
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_in_punch
  ON punches (employee_id, punch_date)
  WHERE punch_type = 'in' AND active = true;
```

### Webhook handler (`workio/server/src/routes/line.ts`)
Use atomic transaction + idempotency key + defensive state handling:

```ts
import { Request, Response } from 'express';
import { pool } from '../db';

export async function handleLineWebhook(req: Request, res: Response) {
  const idempotencyKey = req.headers['x-line-idempotency-key'] || req.body.idempotencyKey;
  const { userId, type, timestamp } = req.body.events?.[0] || {};

  if (!idempotencyKey) {
    return res.status(400).json({ error: 'Missing idempotency key' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // 1) Idempotency check (fast, prevents replay)
    const idem = await client.query(
      `SELECT 1 FROM webhook_idempotency WHERE idempotency_key = $1`,
      [idempotencyKey]
    );
    if (idem.rows.length > 0) {
      await client.query('COMMIT');
      return res.status(200).json({ ok: true, reason: 'duplicate' });
    }

    // 2) Resolve employee
    const emp = await client.query(
      `SELECT id FROM employees WHERE line_user_id = $1`,
      [userId]
    );
    if (emp.rows.length === 0) {
      await client.query('ROLLBACK');
      return res.status(404).json({ error: 'Employee not found' });
    }
    const employeeId = emp.rows[0].id;
    const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD

    // 3) Stateful, atomic handling
    if (type === 'clock_in') {
      // Close any dangling active clock-in (defensive) then insert new one.
      // Unique index prevents races from creating two active 'in' rows.
      await client.query(
        `UPDATE punches
         SET active = false, updated_at = NOW()
         WHERE employee_id = $1 AND punch_date = $2 AND punch_type = 'in' AND active = true`,
        [employeeId, today]
      );

      await client.query(
        `INSERT INTO punches (employee_id, punch_date, punch_type, punch_time, active, source_event_id, created_at, updated_at)
         VALUES ($1, $2, 'in', NOW(), true, $3, NOW(), NOW())`,
        [employeeId, today, idempotencyKey]
      );
    } else if (type === 'clock_out') {
      // Close the active clock-in for the day
      const result = await client.query(
        `UPDATE punches
         SET active = false, updated_at = NOW()
         WHERE employee_id = $1 AND punch_date = $2 AND punch_type = 'in' AND active = true
         RETURNING id`,
        [employeeId, today]
      );

      if (result.rowCount === 0) {
        await client.query('ROLLBACK');
        return res.status(400).json({ error: 'No active clock-in to clock out' });
      }

      // Optional: create a clock-out row for reporting/history
      await client.query(
        `INSERT INTO punches (employee_id, punch_date, punch_type, punch_time, active, source_event_id, created_at, updated_at)
         VALUES ($1, $2, 'out', NOW(), false, $3, NOW(), NOW())`,
        [employeeId, today, idempotencyKey]
      );
    } else {
      await client.query('ROLLBACK');
      return res.status(400).json({ error: 'Unsupported event type' });
    }

    // 4) Record idempotency key
    await client.query(
      `INSERT INTO webhook_idempotency (idempotency_key, event_type, employee_id)
       VALUES ($1, $2, $3)`,
      [idempotencyKey, type, employeeId]
    );

    await client.query('COMMIT');
    return res.status(200).json({ ok: true });
  } catch (err: any) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    // Unique violation or serialization failure -> safe to acknowledge as duplicate/processed
    if (err.code === '23505' || err.code === '40001') {
      return res.status(200).json({ ok: true, reason: 'duplicate' });
    }
    return res.status(500).json({ error: 'Processing failed' });
  } finally {
    client.release();
  }
}
```

### Optional cleanup job
Prune old idempotency keys (e.g., older than 48 hours) via cron or `pg_cron`:

```sql
DELETE FROM webhook_idempotency WHERE created_at < NOW() - INTERVAL '48 hours';
```

### Verification checklist
1. Apply schema changes to the database.
2. Send a webhook with a unique `x-line-idempotency-key` → punch row created, response `200 OK`.
3. Replay same request with same key → no new punch row, response `{ ok: true, reason: "duplicate" }`.
4. Concurrent requests with same key/employee/day → only one active clock-in row exists (unique index + transaction prevents races).
5. Double clock-in without clock-out → second clock-in closes previous active row and creates a new one; no duplicate active rows.
6. Clock-out without active clock-in → `400` error (or adapt policy if desired).
7. Confirm constraint:
   ```sql
   SELECT * FROM punches
   WHERE employee_id = <id> AND punch_date = 'YYYY-MM-DD'
     AND punch_type = 'in' AND active = true;
   ```
   Returns at most one row.
