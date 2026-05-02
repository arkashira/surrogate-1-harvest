# workio / discovery

## Final Synthesized Solution

**Chosen approach:** Atomic upsert at the database layer + persisted idempotency key + transactional webhook handling.  
**Why:** Both candidates correctly identify the same root causes (non-atomic read-then-insert, missing uniqueness constraints, no idempotency key, non-transactional side effects). Candidate 1 provides the most complete, production-ready code and verification steps; Candidate 2 reinforces the same constraints and adds useful migration hygiene. I merge the strongest, most actionable parts and resolve ambiguities in favor of correctness and deployability.

---

### 1. Diagnosis (merged)

- **Race condition / non-atomic upsert**: current path is read-then-insert, allowing duplicate `(employee_id, punch_type, punch_date)` under concurrency or LINE redelivery.
- **No idempotency key persisted**: `deliveryId`/`messageId` is not stored, so retries/redeliveries create extra rows.
- **Missing DB-level guard**: no unique constraint to enforce one punch per employee/type/date; application checks can be bypassed.
- **Non-transactional webhook side effects**: punch creation and acknowledgements are not atomic with idempotency state; partial failures + retries amplify duplicates.
- **Client retry amplification**: LINE retries with no short-circuit for already-processed deliveries.

---

### 2. Proposed change (scope)

- **File**: `workio/server/src/routes/line/webhook.ts` (webhook handler)
- **DB migration**: add `line_delivery_id` (and optionally `line_message_id`) and enforce uniqueness at the database layer.
- **Behavior**:
  - Use `line_delivery_id` as the primary idempotency key.
  - Enforce one punch per `(employee_id, punch_type, DATE(punch_time))` via a unique index.
  - Perform idempotency check and punch upsert atomically in a single transaction.
  - Keep webhook handler fast: commit DB transaction before any external I/O (e.g., LINE reply).

---

### 3. Implementation

#### 3.1 DB schema change (run once)

```sql
-- workio/server/src/db/migrations/001_line_idempotency.sql
-- Add idempotency columns
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS line_delivery_id VARCHAR(255),
  ADD COLUMN IF NOT EXISTS line_message_id VARCHAR(255);

-- Natural-key uniqueness: one punch per employee/type/date
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_employee_type_date
  ON punches (employee_id, punch_type, DATE(punch_time));

-- Idempotency index: prevent re-processing same delivery
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_delivery_id
  ON punches (line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;
```

Notes:
- Use `line_delivery_id` as the authoritative idempotency key.
- `line_message_id` is optional but useful for traceability/debugging.
- Index on `DATE(punch_time)` ensures date-level uniqueness (not timestamp-level).

---

#### 3.2 Webhook handler (atomic upsert + idempotency)

```ts
// workio/server/src/routes/line/webhook.ts
import { Request, Response } from 'express';
import { pool } from '../../db';
import { verifyLineSignature } from '../../utils/line';
import { replyToLine } from '../../utils/line/client';

export async function handleLineWebhook(req: Request, res: Response) {
  try {
    const sig = req.headers['x-line-signature'] as string;
    const body = req.body;

    if (!verifyLineSignature(JSON.stringify(body), sig)) {
      return res.status(401).send('Invalid signature');
    }

    const events = body.events || [];
    // Process serially per-event to preserve ordering; parallelize only if safe
    for (const ev of events) {
      if (ev.type === 'message' && ev.message.type === 'text') {
        // Fire-and-forget after commit; don't block response on external I/O
        handlePunchMessage(ev)
          .catch((err) => console.error('[handlePunchMessage] unhandled', err));
      }
    }

    // Acknowledge quickly to avoid LINE retries
    res.status(200).send('OK');
  } catch (err) {
    console.error('[line-webhook] error', err);
    res.status(500).send('Server error');
  }
}

async function handlePunchMessage(event: any) {
  const userId = event.source.userId;
  const deliveryId = event.deliveryId || event.message?.id;
  const messageId = event.message?.id || null;
  const text = (event.message?.text || '').trim().toLowerCase();

  if (!deliveryId) {
    console.warn('[handlePunchMessage] missing deliveryId', event);
    return;
  }

  const punchType = text === 'out' || text === 'clock out' ? 'out' : 'in';
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check (fast)
    const idempotent = await client.query(
      `SELECT 1 FROM punches WHERE line_delivery_id = $1 FOR UPDATE`,
      [deliveryId]
    );
    if (idempotent.rows.length > 0) {
      await client.query('COMMIT');
      return;
    }

    // Get or create employee by line user id
    let emp = await client.query(
      `SELECT id FROM employees WHERE line_user_id = $1 FOR UPDATE`,
      [userId]
    );

    if (emp.rows.length === 0) {
      const insertEmp = await client.query(
        `INSERT INTO employees (line_user_id, name, tenant_id)
         VALUES ($1, $2, $3) RETURNING id`,
        [userId, `Employee ${userId}`, 1] // default tenant; adjust as needed
      );
      emp = { rows: [insertEmp.rows[0]] };
    }

    const employeeId = emp.rows[0].id;
    const now = new Date();

    // Atomic upsert: prefer existing natural key row, else insert
    await client.query(
      `INSERT INTO punches (employee_id, punch_type, punch_time, line_delivery_id, line_message_id, created_at, updated_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7)
       ON CONFLICT (employee_id, punch_type, DATE(punch_time))
       DO UPDATE SET
         punch_time = EXCLUDED.punch_time,
         line_delivery_id = COALESCE(punches.line_delivery_id, EXCLUDED.line_delivery_id),
         line_message_id = EXCLUDED.line_message_id,
         updated_at = EXCLUDED.updated_at`,
      [employeeId, punchType, now, deliveryId, messageId, now, now]
    );

    await client.query('COMMIT');

    // Non-blocking external I/O after commit
    // Best-effort: do not throw into transaction; log failures only
    replyToLine(event.replyToken, `Punch ${punchType} recorded at ${now.toISOString()}`)
      .catch((err) => console.error('[replyToLine] failed', err));
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('[handlePunchMessage] failed', err);
    throw err;
  } finally {
    client.release();
  }
}
```

Key decisions:
- **Idempotency first**: check `line_delivery_id` with `FOR UPDATE` inside the transaction to prevent races.
- **Atomic upsert**: use `ON CONFLICT (employee_id, punch_type, DATE(punch_time))` to guarantee uniqueness at the DB level.
- **Preserve first delivery**: `COALESCE(punches.line_delivery_id, EXCLUDED.line_delivery_id)` keeps the original idempotency key if a row already exists.
- **Fast ACK**: respond `200 OK` to LINE immediately after queueing processing (or after commit if processing is inline) to minimize retries.
- **External I/O after commit**: reply to LINE outside the transaction to avoid holding locks and to prevent partial failures from blocking retries.

---

#### 3.3 Small infra/ops notes

- **Connection pool**: configure sensible `max` connections and timeouts for `pool`.
- **Missing deliveryId**: log warnings and reject processing if `deliveryId` is absent (cannot guarantee idempotency).
- **Webhook throughput**: process events serially per user or use a
