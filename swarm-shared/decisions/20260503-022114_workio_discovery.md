# workio / discovery

## Final synthesized solution (correct + actionable)

**Core diagnosis (merged, de-duplicated)**
- LINE webhook retries are at-least-once and create duplicate punches.
- The punch upsert is non-atomic (`findOne` → conditional `insert`/`update`) and races under concurrency.
- There is no strong DB-level idempotency or uniqueness to prevent duplicates across retries or races.
- Tenant isolation must be enforced at the DB level (not only in app code).

**Chosen approach (resolve contradictions in favor of correctness + actionability)**
- Use **two complementary DB constraints**:
  1. Idempotency on `tenant_id + line_message_id` (guarantees retries with same message never double-insert).
  2. Business rule uniqueness on `tenant_id + employee_id + punch_date + event_type` (guarantees one logical punch per window, regardless of message id).
- Perform ingestion in **one transaction** with an atomic upsert that checks both constraints.
- Keep the schema simple and migration-friendly: add columns and constraints to `punches` (preferred) unless audit/legal requires a separate dedup table — in which case use a small `punch_message_dedup` table keyed by `(tenant_id, message_id)`.

---

### 1) Schema changes (run once)

File: `/opt/axentx/workio/server/src/db/schema.sql`

```sql
-- Add idempotency columns to punches (nullable to avoid breaking existing rows)
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS line_message_id VARCHAR(255),
  ADD COLUMN IF NOT EXISTS line_timestamp BIGINT;

-- Idempotency: one LINE message per tenant (only applies when line_message_id is present)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_tenant_line_message
  ON punches (tenant_id, line_message_id)
  WHERE line_message_id IS NOT NULL;

-- Business rule: one punch per tenant/employee/date/event_type window
-- (adjust if you allow multiple punches per day; this prevents duplicates/races)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_tenant_employee_date_event
  ON punches (tenant_id, employee_id, punch_date, event_type);

-- Optional helper indexes
CREATE INDEX IF NOT EXISTS idx_punches_tenant_employee
  ON punches (tenant_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_punches_line_message_lookup
  ON punches (line_message_id)
  WHERE line_message_id IS NOT NULL;
```

If you prefer a separate dedup table (e.g., to avoid touching `punches` history), use this instead of `line_message_id` on `punches`:

```sql
CREATE TABLE IF NOT EXISTS punch_message_dedup (
  tenant_id  BIGINT NOT NULL,
  message_id TEXT   NOT NULL,
  punch_id   BIGINT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id, message_id)
);
```

---

### 2) Webhook handler (atomic, idempotent, tenant-safe)

File (choose the actual path in your repo; example below):  
`/opt/axentx/workio/server/src/routes/line/webhook.ts`  
or  
`/opt/axentx/workio/server/src/controllers/lineWebhookController.ts`

```ts
import { Request, Response } from 'express';
import { pool } from '../db';
import { verifyLineSignature } from '../utils/line';

export async function handleLinePunchEvent(req: Request, res: Response) {
  const {
    tenantId,
    employeeId,
    eventType,
    punchDate,
    messageId,
    lineTimestamp,
    location,
    ...payload
  } = req.body;

  if (!tenantId || !employeeId || !messageId || !eventType || !punchDate) {
    return res.status(400).json({ error: 'Missing required fields' });
  }

  // Optional: verify LINE signature if raw LINE webhook
  // if (!verifyLineSignature(req.headers['x-line-signature'], req.rawBody)) {
  //   return res.status(401).json({ error: 'Invalid signature' });
  // }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency path A: if using punch_message_dedup table
    // const dedup = await client.query(
    //   `SELECT punch_id FROM punch_message_dedup WHERE tenant_id = $1 AND message_id = $2`,
    //   [tenantId, messageId]
    // );
    // if (dedup.rows.length > 0) {
    //   const existing = await client.query(
    //     `SELECT * FROM punches WHERE id = $1 AND tenant_id = $2`,
    //     [dedup.rows[0].punch_id, tenantId]
    //   );
    //   await client.query('COMMIT');
    //   return res.json({ punch: existing.rows[0], deduped: true });
    // }

    // Idempotency path B: if using line_message_id column on punches (preferred, simpler)
    const existingByMessage = await client.query(
      `SELECT * FROM punches WHERE tenant_id = $1 AND line_message_id = $2`,
      [tenantId, messageId]
    );
    if (existingByMessage.rows.length > 0) {
      await client.query('COMMIT');
      return res.json({ punch: existingByMessage.rows[0], deduped: true });
    }

    // Atomic upsert using business uniqueness (tenant+employee+date+event_type)
    // If a conflict occurs, update only mutable fields (e.g., location, line_message_id if absent)
    const upsert = await client.query(
      `INSERT INTO punches (
         tenant_id,
         employee_id,
         punch_date,
         event_type,
         line_message_id,
         line_timestamp,
         location,
         created_at,
         updated_at
       ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
       ON CONFLICT (tenant_id, employee_id, punch_date, event_type)
       DO UPDATE SET
         line_message_id = COALESCE(punches.line_message_id, EXCLUDED.line_message_id),
         line_timestamp  = COALESCE(punches.line_timestamp, EXCLUDED.line_timestamp),
         location        = EXCLUDED.location,
         updated_at      = NOW()
       RETURNING *`,
      [tenantId, employeeId, punchDate, eventType, messageId, lineTimestamp, location]
    );

    const punch = upsert.rows[0];

    // If using separate dedup table, record it here:
    // await client.query(
    //   `INSERT INTO punch_message_dedup (tenant_id, message_id, punch_id)
    //    VALUES ($1, $2, $3)`,
    //   [tenantId, messageId, punch.id]
    // );

    await client.query('COMMIT');
    return res.json({ punch, deduped: false });
  } catch (err: any) {
    await client.query('ROLLBACK');

    // If conflict on line_message_id unique partial index (race between two messages for same logical punch),
    // treat as idempotent and return existing punch.
    if (err.code === '23505' && err.constraint && err.constraint.includes('idx_punches_tenant_line_message')) {
      const fallback = await client.query(
        `SELECT * FROM punches WHERE tenant_id = $1 AND line_message_id = $2`,
        [tenantId, messageId]
      );
      if (fallback.rows.length > 0) {
        return res.json({ punch: fallback.rows[0], deduped: true });
      }
    }

    console.error('Punch handling failed', err);
    return res.status(500).json({ error: 'Internal server error' });
  } finally {
    client.release();
  }
}
```

Notes on handler choices
- Prefer the `line_message_id` column + partial unique index (simpler, fewer tables). Use the separate `punch_message_dedup` table only if you must avoid altering `punches`.
- The `ON CONFLICT` uses the business uniqueness index; it safely serializes races and prevents duplicate logical punches.
- `COALESCE` on update preserves the first `line_message_id`/`line_timestamp` if already set (idempotency).
- Tenant isolation is enforced by including `tenant_id` in all WHERE/ON CONFLICT/index clauses and in the dedup key.

---

### 3) Verification steps
