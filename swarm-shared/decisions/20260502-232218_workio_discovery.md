# workio / discovery

## Final Synthesis & Actionable Fix

**Core problem**: LINE webhook redeliveries and concurrent deliveries create duplicate punch rows because creation is non-atomic and there is no database-level idempotency guard.

**Chosen approach**:
- Use **LINE message ID** as the idempotency key (it uniquely identifies the originating event).
- Add **audit columns** (`line_message_id`, `webhook_delivery_id`, `created_source`) to enable tracing and safe deduplication.
- Enforce uniqueness at the **database layer** with a partial unique index on `line_message_id` (where not null) so duplicates are rejected atomically.
- Replace read-then-insert with an **atomic `INSERT … ON CONFLICT (line_message_id) DO NOTHING`** inside a transaction.
- Keep punch-type logic (toggle in/out) inside the same transaction so concurrent deliveries cannot observe stale state.
- Return **200 OK only after commit** so LINE stops retrying failed deliveries.

---

## 1. DB schema change  
File: `workio/server/src/db/schema.sql`

```sql
-- Audit/idempotency columns
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS line_message_id TEXT,
  ADD COLUMN IF NOT EXISTS webhook_delivery_id TEXT,
  ADD COLUMN IF NOT EXISTS created_source TEXT NOT NULL DEFAULT 'line';

-- Idempotency guard: one row per LINE message
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_message
  ON punches (line_message_id)
  WHERE line_message_id IS NOT NULL;

-- Optional: if business rule is exactly one in+out per employee per day, uncomment:
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_employee_date_type
--   ON punches (employee_id, punch_date, punch_type);
```

Apply:

```bash
cd /opt/axentx/workio
psql workio < server/src/db/schema.sql
```

---

## 2. Idempotent webhook handler  
File: `workio/server/src/routes/line/webhook.ts` (or `line.ts` if that is the actual path)

```ts
import { Request, Response } from 'express';
import { pool } from '../../db';

export async function handleLineWebhook(req: Request, res: Response) {
  const { events } = req.body;
  const deliveryId = (req.headers['x-line-delivery-id'] as string) || `local-${Date.now()}`;

  if (!events || !Array.isArray(events)) {
    return res.status(400).json({ error: 'invalid payload' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      if (ev.type !== 'message' || !ev.source?.userId) continue;

      const lineMessageId = ev.message?.id;
      if (!lineMessageId) continue;

      // Idempotency: skip if already processed (fast path via unique index)
      const exists = await client.query(
        `SELECT 1 FROM punches WHERE line_message_id = $1 LIMIT 1`,
        [lineMessageId]
      );
      if (exists.rows.length > 0) {
        continue;
      }

      const employeeId = await getEmployeeIdByLineUserId(client, ev.source.userId);
      if (!employeeId) continue;

      const punchDate = new Date(ev.timestamp || Date.now()).toISOString().split('T')[0];
      const punchType = await determinePunchType(client, employeeId, punchDate);

      await client.query(
        `INSERT INTO punches (employee_id, punch_date, punch_type, line_message_id, webhook_delivery_id, created_source)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (line_message_id) DO NOTHING`,
        [employeeId, punchDate, punchType, lineMessageId, deliveryId, 'line']
      );
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    res.status(500).json({ error: 'processing_failed' });
  } finally {
    client.release();
  }
}

async function getEmployeeIdByLineUserId(client: any, lineUserId: string) {
  const r = await client.query(
    `SELECT id FROM employees WHERE line_user_id = $1 LIMIT 1`,
    [lineUserId]
  );
  return r.rows[0]?.id || null;
}

async function determinePunchType(client: any, employeeId: number, punchDate: string) {
  // Toggle: if last punch today is 'in', next is 'out'; otherwise 'in'
  const r = await client.query(
    `SELECT punch_type FROM punches
     WHERE employee_id = $1 AND punch_date = $2
     ORDER BY created_at DESC LIMIT 1`,
    [employeeId, punchDate]
  );
  const last = r.rows[0];
  return last?.punch_type === 'in' ? 'out' : 'in';
}
```

---

## 3. Verification checklist

1. **Schema applied**
   ```bash
   psql workio -c "\d punches"
   # Confirm line_message_id and idx_punches_line_message exist
   ```

2. **Single delivery produces one row**
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "x-line-delivery-id: test-delivery-123" \
     -d '{
       "events": [
         {
           "type": "message",
           "message": { "id": "1234567890", "type": "text" },
           "source": { "userId": "U1234567890abcdef" },
           "timestamp": 1712345678901
         }
       ]
     }'
   ```

3. **Duplicate deliveries produce exactly one row**
   Repeat same request 2–3 times, then:
   ```bash
   psql workio -c "SELECT id, employee_id, punch_date, punch_type, line_message_id FROM punches WHERE line_message_id='1234567890';"
   ```
   Expect exactly one row.

4. **Concurrent deliveries produce exactly one row**
   ```bash
   for i in {1..10}; do
     curl -s -X POST http://localhost:3000/webhook/line \
       -H "Content-Type: application/json" \
       -H "x-line-delivery-id: concurrent-test-123" \
       -d '{"events":[{"type":"message","message":{"id":"9999999999","type":"text"},"source":{"userId":"U1234567890abcdef"},"timestamp":1712345678901}]}' &
   done
   wait
   psql workio -c "SELECT count(*) FROM punches WHERE line_message_id='9999999999';"
   ```
   Expect count = 1.

5. **Behavior for multiple punches per day**
   - Send two different `line_message_id` values for the same employee/day → expect two rows (in + out) if your toggle logic allows it.
   - If you enforce one in+out per day, uncomment the `(employee_id, punch_date, punch_type)` unique index and adjust upsert/conflict target accordingly.

---

**Why this wins**:
- **Correctness**: uniqueness enforced at DB level (survives concurrent deliveries).
- **Actionability**: clear schema + handler change + verification steps.
- **Traceability**: audit columns let you debug delivery retries and origin.
- **Safety**: transaction + `ON CONFLICT DO NOTHING` prevents partial writes and duplicates.
