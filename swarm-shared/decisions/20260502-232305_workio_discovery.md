# workio / discovery

## Final synthesized answer (strongest, correct, actionable)

### 1. Root cause (concise)
- **Non-atomic read-then-insert** + **no uniqueness guard** + **LINE redeliveries/concurrency** = duplicate punches for `(employee_id, punch_type, punch_date)`.
- Missing transactional upsert and idempotency allows duplicates under concurrency and webhook replay.

### 2. Recommended solution (single atomic path)
Adopt **one** deterministic, race-safe mechanism and keep it simple:

**Preferred primary defense** (schema-level correctness):  
Add a **natural unique constraint** on `punches(employee_id, punch_type, punch_date)` and use **atomic `INSERT ... ON CONFLICT DO NOTHING` (or upsert)** in a single transaction per webhook.  

**Optional secondary defense** (webhook replay safety):  
Add a lightweight `line_webhook_idempotency` table keyed by a deterministic idempotency key (e.g., `line:{userId}:{messageId}:{punchType}`) to suppress LINE redeliveries before punch logic runs. This is strictly additive and useful if you must accept replays safely during incidents or retries.

Do **not** mix both without clear ownership — the constraint is the source-of-truth correctness guard; the idempotency table is a replay filter.

### 3. Implementation (concrete)

#### 3.1 Schema (run once)
```sql
-- Primary correctness guard
ALTER TABLE punches
  ADD CONSTRAINT uniq_employee_punch_date
  UNIQUE (employee_id, punch_type, punch_date);

-- Optional: webhook replay filter
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  message_id      TEXT NOT NULL,
  employee_id     INTEGER NOT NULL,
  punch_type      TEXT NOT NULL,
  punch_date      DATE NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_line_webhook_idempotency_created_at ON line_webhook_idempotency(created_at);
```

#### 3.2 DB helper (atomic upsert)
```ts
// server/src/db/queries/punchQueries.ts
import { PoolClient } from 'pg';

export async function upsertPunch(
  client: PoolClient,
  { employee_id, punch_type, punch_date, location, notes }: {
    employee_id: number;
    punch_type: 'clock_in' | 'clock_out';
    punch_date: Date;
    location?: { lat: number; lng: number };
    notes?: string;
  }
) {
  const result = await client.query(
    `INSERT INTO punches (employee_id, punch_type, punch_date, location, notes)
     VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (employee_id, punch_type, punch_date)
     DO UPDATE SET
       location = EXCLUDED.location,
       notes = EXCLUDED.notes,
       updated_at = NOW()
     RETURNING *`,
    [employee_id, punch_type, punch_date, location ? JSON.stringify(location) : null, notes || null]
  );
  return result.rows[0];
}
```

#### 3.3 Idempotency helper (optional)
```ts
// server/src/db/queries/lineIdempotencyQueries.ts
import { PoolClient } from 'pg';

export async function tryAcquireWebhook(
  client: PoolClient,
  { idempotency_key, message_id, employee_id, punch_type, punch_date }: {
    idempotency_key: string;
    message_id: string;
    employee_id: number;
    punch_type: 'clock_in' | 'clock_out';
    punch_date: Date;
  }
) {
  const result = await client.query(
    `INSERT INTO line_webhook_idempotency (idempotency_key, message_id, employee_id, punch_type, punch_date)
     VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (idempotency_key) DO NOTHING
     RETURNING *`,
    [idempotency_key, message_id, employee_id, punch_type, punch_date]
  );
  return result.rowCount > 0; // true = newly acquired (not seen before)
}
```

#### 3.4 LINE webhook handler (atomic tx + idempotency)
```ts
// server/src/routes/line.ts
import { Router } from 'express';
import { pool } from '../db';
import { upsertPunch } from '../db/queries/punchQueries';
import { tryAcquireWebhook } from '../db/queries/lineIdempotencyQueries';

const router = Router();

router.post('/webhook/line', async (req, res) => {
  const sig = req.headers['x-line-signature'] as string || '';
  const body = req.body;

  if (!body.events || !Array.isArray(body.events)) {
    return res.status(400).send('Invalid payload');
  }

  // Optional: verify LINE signature here with channel secret

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of body.events) {
      if (ev.type !== 'message' || ev.message.type !== 'text') continue;

      const employeeRow = await client.query(
        'SELECT id FROM employees WHERE line_user_id = $1',
        [ev.source.userId]
      );
      if (employeeRow.rowCount === 0) continue;

      const employee_id = employeeRow.rows[0].id;
      const text = ev.message.text.trim().toLowerCase();

      let punch_type: 'clock_in' | 'clock_out' | null = null;
      if (['เข้างาน', 'in', 'clock in'].includes(text)) punch_type = 'clock_in';
      if (['เลิกงาน', 'out', 'clock out'].includes(text)) punch_type = 'clock_out';
      if (!punch_type) continue;

      const punch_date = new Date(); // or parse from message if needed

      // Optional idempotency guard against LINE redelivery
      const idempotency_key = `line:${ev.source.userId}:${ev.message.id}:${punch_type}`;
      const acquired = await tryAcquireWebhook(client, {
        idempotency_key,
        message_id: ev.message.id,
        employee_id,
        punch_type,
        punch_date
      });
      if (!acquired) continue; // already processed this webhook event

      const location = ev.message.location
        ? { lat: ev.message.location.latitude, lng: ev.message.location.longitude }
        : undefined;

      await upsertPunch(client, { employee_id, punch_type, punch_date, location });
    }

    await client.query('COMMIT');
    return res.status(200).send('OK');
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook tx failed', err);
    return res.status(500).send('Server error');
  } finally {
    client.release();
  }
});

export default router;
```

### 4. Operational notes
- **Indexing**: Ensure `punches(employee_id, punch_type, punch_date)` is indexed (the unique constraint already provides this). Also index `employees(line_user_id)` for fast lookup.
- **Cleanup**: If you use `line_webhook_idempotency`, rotate/expire rows older than 7–30 days via cron or DB job.
- **Signature verification**: Add LINE signature verification (`x-line-signature`) with your channel secret before processing in production.
- **Testing**:
  - Send the same LINE payload twice and confirm only one punch row is created.
  - Simulate concurrent deliveries (parallel requests) and confirm no duplicates.
  - Verify constraint exists in DB (`\d punches` in psql).

### 5. Why this is correct and actionable
- **Correctness**: The unique constraint is the source-of-truth guard against duplicates regardless of application race or redelivery.
- **Actionability**: Each change is minimal, isolated, and testable. You can deploy the constraint + upsert immediately; the idempotency table is optional and additive.
- **Safety**: All writes occur inside a transaction; failures roll back cleanly. Idempotency key prevents wasted work on replays.
