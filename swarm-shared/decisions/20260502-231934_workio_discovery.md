# workio / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

### Diagnosis (merged, corrected)
- **LINE webhook redeliveries have no idempotency** → duplicate punch rows for same `(employee_id, punch_type, date)` on retries.
- **Punch creation is read-then-insert (non-atomic)** → race windows allow multiple active punches for the same employee/date.
- **No storage-level uniqueness** → DB cannot prevent duplicates when app logic races.
- **No idempotency key tied to LINE event identifiers** → retries cannot be safely de-duplicated.
- **Edge-case handling missing** (double clock-in should update the active punch, not insert; clock-out with no active punch should be handled predictably).

---

### Proposed Change (merged)
- Add an idempotency table keyed by LINE event identifiers.
- Add a **partial unique constraint/index** to enforce at most one active punch per employee per day.
- Refactor webhook handler to use **atomic upsert/update within a transaction** and record idempotency **after successful punch handling** (to allow safe retry on failure).
- Keep defensive logic: double clock-in updates the active punch; clock-out updates the latest active punch (or inserts a paired record if policy requires).

---

### Implementation

#### 1) Schema changes  
File: `/opt/axentx/workio/server/src/db/schema.sql`

```sql
-- Idempotency table for LINE webhook deliveries
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_type      TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  delivery_id     TEXT NOT NULL,
  handled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ensure at most one active punch per employee per calendar day.
-- Adjust column names to match your punches table.
-- Assumes:
--   punches.id, employee_id, punch_type, timestamp, tenant_id, clock_out_at, deleted_at
-- Active = clock_out_at IS NULL AND deleted_at IS NULL
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_punch_per_employee
ON punches (employee_id, DATE(timestamp))
WHERE clock_out_at IS NULL AND deleted_at IS NULL;
```

#### 2) Atomic webhook handler  
File: `/opt/axentx/workio/server/src/routes/line/webhook.ts` (or `.js`)

```ts
import { Request, Response } from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

function buildIdempotencyKey(event: any): string {
  // Use deliveryId + type + userId to avoid cross-event collisions.
  // Include a short hash of payload for extra safety if desired.
  const payloadHash = crypto
    .createHash('sha256')
    .update(JSON.stringify(event.message || {}))
    .digest('hex')
    .slice(0, 12);
  return `line:${event.deliveryId}:${event.type}:${event.source.userId}:${payloadHash}`;
}

export async function handleLineWebhook(req: Request, res: Response) {
  const events = req.body.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).send('No events');
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      const key = buildIdempotencyKey(ev);
      const { type, source, deliveryId } = ev;

      // Skip if already successfully handled (idempotent)
      const dup = await client.query(
        `SELECT 1 FROM line_webhook_idempotency WHERE idempotency_key = $1`,
        [key]
      );
      if (dup.rows.length > 0) continue;

      // Only process message events with text commands
      if (type === 'message' && ev.message?.type === 'text') {
        const text = (ev.message.text || '').trim().toLowerCase();
        const userId = source.userId;

        // Resolve LINE userId -> employee
        const empRes = await client.query(
          `SELECT id, tenant_id FROM employees WHERE line_user_id = $1 LIMIT 1`,
          [userId]
        );
        if (empRes.rows.length === 0) continue;
        const employeeId = empRes.rows[0].id;
        const tenantId = empRes.rows[0].tenant_id;

        const isClockIn = text.includes('in') || text.includes('เข้า');
        const isClockOut = text.includes('out') || text.includes('ออก');
        if (!isClockIn && !isClockOut) continue;

        const now = new Date();

        if (isClockIn) {
          // Atomic upsert: if an active punch exists, update it; otherwise insert.
          // This avoids duplicates and handles double clock-in gracefully.
          await client.query(`
            INSERT INTO punches (employee_id, punch_type, timestamp, tenant_id, clock_out_at, created_at)
            VALUES ($1, $2, $3, $4, NULL, $3)
            ON CONFLICT (employee_id, DATE(timestamp))
            DO UPDATE SET
              punch_type = EXCLUDED.punch_type,
              timestamp = EXCLUDED.timestamp,
              clock_out_at = NULL
            WHERE punches.clock_out_at IS NULL
          `, [employeeId, 'in', now, tenantId]);
        } else {
          // Clock out: update the latest active punch for this employee.
          // If none exists, optionally insert a paired in+out record depending on policy.
          const up = await client.query(`
            UPDATE punches
            SET clock_out_at = $1, punch_type = 'out'
            WHERE employee_id = $2 AND clock_out_at IS NULL AND deleted_at IS NULL
            ORDER BY timestamp DESC
            LIMIT 1
            RETURNING id
          `, [now, employeeId]);

          // Optional: if no active punch, insert a closed punch (policy-dependent)
          if (up.rowCount === 0) {
            await client.query(`
              INSERT INTO punches (employee_id, punch_type, timestamp, tenant_id, clock_out_at, created_at)
              VALUES ($1, 'out', $2, $3, $2, $2)
            `, [employeeId, now, tenantId]);
          }
        }
      }

      // Record idempotency AFTER successful punch handling (allows retry on failure)
      await client.query(
        `INSERT INTO line_webhook_idempotency (idempotency_key, event_type, user_id, delivery_id)
         VALUES ($1, $2, $3, $4)`,
        [key, type, source.userId, deliveryId]
      );
    }

    await client.query('COMMIT');
    res.status(200).send('OK');
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    res.status(500).send('Error');
  } finally {
    client.release();
  }
}
```

---

### Verification (actionable)

1. Apply schema:
   ```bash
   psql workio < /opt/axentx/workio/server/src/db/schema.sql
   ```
2. Restart backend:
   ```bash
   cd /opt/axentx/workio/server && npm run dev
   ```
3. Duplicate-delivery test:
   - Send a valid clock-in payload twice (same `deliveryId`) to the webhook.
   - Verify only one active punch exists:
     ```sql
     SELECT * FROM punches
     WHERE employee_id = <test_employee>
     ORDER BY created_at DESC LIMIT 5;
     ```
4. Race-condition test:
   - Use a parallel tool (e.g., `ab`, `hey`, or a small Node script) to POST the same payload 20 times concurrently.
   - Confirm:
     - No constraint violations.
     - Exactly one active punch per employee per day.
5. Idempotency table:
   ```sql
   SELECT * FROM line_webhook_idempotency ORDER BY created_at DESC LIMIT 10;
   ```
6. Edge-case checks:
   - Double clock-in → updates existing active punch (no duplicate).
   - Clock-out with no active punch → inserts a closed punch (or logs per policy).
   - Mixed retries and failures → idempotency prevents duplicates after success; failures allow safe retry.
