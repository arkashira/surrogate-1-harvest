# workio / discovery

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, I will analyze the provided candidates and resolve any contradictions in favor of correctness and concrete actionability.

**Diagnosis:**
Both candidates identify the same issues:
1. Duplicate punch rows are created due to LINE webhook redeliveries because of the lack of an idempotency key or deduplication guard.
2. Non-atomic read-then-insert operations allow race conditions between concurrent or redelivered webhooks, resulting in duplicate active punches for the same employee/date.
3. The absence of a unique constraint or upsert path in the DB schema fails to enforce one active punch per employee per date.

**Proposed Change:**
To address these issues, the proposed changes include:
1. Adding a unique constraint to the `punches` table to prevent duplicate active punches per employee/date.
2. Implementing atomic upsert with an idempotency key extracted from the LINE `deliveryId`/`timestamp` composite and using `ON CONFLICT DO NOTHING`.
3. Creating an idempotency table to store processed webhook messages and prevent duplicate processing.

**Implementation:**
The implementation involves the following steps:
### Step 1: Add Unique Constraint
Add a unique constraint to the `punches` table to prevent duplicate active punches per employee/date:
```sql
CREATE TABLE IF NOT EXISTS punches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_id UUID NOT NULL REFERENCES employees(id),
    punch_type TEXT NOT NULL,  -- 'in' | 'out'
    punched_at TIMESTAMPTZ NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    line_user_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (employee_id, DATE(punched_at), punch_type)
);
```
If the table already exists, add the constraint safely:
```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_punches_employee_date_type') THEN
        ALTER TABLE punches ADD CONSTRAINT uq_punches_employee_date_type UNIQUE (employee_id, DATE(punched_at), punch_type);
    END IF;
END $$;
```
### Step 2: Implement Idempotent Upsert
Implement atomic upsert with an idempotency key extracted from the LINE `deliveryId`/`timestamp` composite and use `ON CONFLICT DO NOTHING`:
```ts
import { Request, Response } from 'express';
import { pool } from '../db';

// Helper: stable idempotency key from LINE delivery metadata
function lineIdempotencyKey(body: any): string {
    const deliveryId = body?.events?.[0]?.deliveryId;
    if (deliveryId) return `line:${deliveryId}`;
    const ev = body?.events?.[0];
    return `line:${ev?.timestamp}:${ev?.source?.userId}:${ev?.type}`;
}

export async function handleLineWebhook(req: Request, res: Response) {
    const body = req.body;
    const events = body?.events;
    if (!Array.isArray(events) || events.length === 0) {
        return res.status(400).json({ error: 'invalid_payload' });
    }

    const client = await pool.connect();
    try {
        await client.query('BEGIN');

        // Idempotency guard: skip if already processed
        const idemKey = lineIdempotencyKey(body);
        const exists = await client.query(`SELECT 1 FROM line_webhook_idempotency WHERE idempotency_key = $1`, [idemKey]);
        if (exists.rows.length > 0) {
            await client.query('COMMIT');
            return res.status(200).json({ ok: true, note: 'duplicate_ignored' });
        }

        // Record idempotency key (best-effort; TTL can be added via pg_cron/policy)
        await client.query(`INSERT INTO line_webhook_idempotency (idempotency_key, created_at) VALUES ($1, NOW())`, [idemKey]);

        // Process each relevant event (simplified: one message event)
        for (const ev of events) {
            if (ev.type !== 'message' || ev.message?.type !== 'text') continue;
            const lineUserId = ev.source?.userId;
            const text = (ev.message.text || '').trim().toLowerCase();
            const isClockIn = text === 'in' || text === 'clock in';
            const isClockOut = text === 'out' || text === 'clock out';
            if (!isClockIn && !isClockOut) continue;

            // Resolve employee by line_user_id (adjust column name as needed)
            const emp = await client.query(`SELECT id FROM employees WHERE line_user_id = $1`, [lineUserId]);
            if (emp.rows.length === 0) continue;
            const employeeId = emp.rows[0].id;
            const punchType = isClockIn ? 'in' : 'out';
            const punchedAt = new Date();

            // Atomic upsert: do nothing on conflict
            await client.query(`INSERT INTO punches (employee_id, punch_type, punched_at, line_user_id) VALUES ($1, $2, $3, $4) ON CONFLICT (employee_id, DATE(punched_at), punch_type) DO NOTHING`, [employeeId, punchType, punchedAt, lineUserId]);
        }

        await client.query('COMMIT');
        res.status(200).json({ ok: true });
    } catch (err) {
        await client.query('ROLLBACK');
        console.error('LINE webhook error', err);
        res.status(500).json({ error: 'server_error' });
    } finally {
        client.release();
    }
}
```
### Step 3: Create Idempotency Table
Create an idempotency table to store processed webhook messages and prevent duplicate processing:
```sql
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
**Verification:**
To verify the implementation, perform the following tests:
1. **Schema check**: Confirm the unique constraint on `(employee_id, date(punched_at), punch_type)` and the primary key on `idempotency_key`.
2. **Duplicate prevention test**: Send the same LINE webhook payload twice and verify that the second request returns `200` with `duplicate_ignored` and no new punch row is inserted.
3. **Race-condition test**: Fire two concurrent requests for the same employee/date/punch_type with different `deliveryId` values and confirm that only one punch row exists.
4. **End-to-end smoke test**: Start the backend, expose the `/webhook/line` endpoint, send a test webhook or message "in" as an employee, and check the DB for the expected punch row.

By following these steps, you can ensure that the LINE webhook handling is idempotent, and duplicate punch rows are prevented.
