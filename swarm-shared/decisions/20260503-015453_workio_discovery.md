# workio / discovery

## 1. Diagnosis

- **No database-level idempotency for LINE webhook punches** — duplicate `X-Line-Signature` + payload retries create multiple clock-in/out rows because uniqueness is enforced only in app logic (`findOne → update/insert`), not at the DB layer.
- **Non-atomic upsert path** — current flow reads then writes, creating race conditions under concurrent LINE retries or parallel webhook deliveries.
- **Missing unique constraint on (`employee_id`, `date`, `type`, `shift_id`)** — allows logically impossible duplicate punches for the same shift/day.
- **No idempotency key column** — cannot reliably de-duplicate retries because payloads may differ slightly (e.g., whitespace, timestamp jitter) while representing the same logical event.
- **No audit trail for webhook deliveries** — no table to store `X-Line-Signature` + hash of body to detect and reject replays quickly.

## 2. Proposed change

Add a single migration + two small code changes in `/opt/axentx/workio/server/src/db/migrations/` and `/opt/axentx/workio/server/src/services/punchService.ts` (or equivalent). Scope:
- Create migration: `add_idempotency_and_constraints.sql`
- Add columns: `idempotency_key` (text, not null), `line_signature` (text), `created_at` default now.
- Add unique constraint: (`idempotency_key`) and (`employee_id`, `date`, `type`, `shift_id`) where applicable.
- Update punch service to use atomic `INSERT ... ON CONFLICT (idempotency_key) DO UPDATE` so duplicates are ignored safely.

## 3. Implementation

### Migration (SQL)

```sql
-- /opt/axentx/workio/server/src/db/migrations/20260503_add_idempotency_and_constraints.sql

-- Add idempotency columns
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS line_signature TEXT,
  ADD COLUMN IF NOT EXISTS payload_hash TEXT;

-- Create deterministic idempotency key index for fast lookup
-- (application should populate idempotency_key as stable hash, e.g., SHA256 of canonical payload)
CREATE INDEX IF NOT EXISTS idx_punches_idempotency_key ON punches (idempotency_key);

-- Unique constraint on idempotency key (exactly-once per logical event)
ALTER TABLE punches
  ADD CONSTRAINT uniq_punch_idempotency UNIQUE (idempotency_key);

-- Prevent logical duplicates per employee/date/type/shift
-- (Allow NULL shift_id for non-shift models)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punch_employee_date_type_shift
ON punches (employee_id, date, type, COALESCE(shift_id, -1))
WHERE deleted_at IS NULL;

-- Optional: store last LINE signature per employee+date for quick replay detection
CREATE INDEX IF NOT EXISTS idx_punch_line_sig ON punches (line_signature);
```

### Service change (TypeScript)

```ts
// /opt/axentx/workio/server/src/services/punchService.ts
import { Pool } from 'pg';
import crypto from 'crypto';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

function buildIdempotencyKey(body: any, lineSignature: string): string {
  // Deterministic canonical payload (sorted keys, no whitespace variance)
  const canonical = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash('sha256').update(canonical).digest('hex');
}

export async function handlePunchWebhook(body: any, lineSignature: string) {
  const idempotencyKey = buildIdempotencyKey(body, lineSignature);
  const employeeId = body.employeeId; // adapt to actual payload shape
  const date = body.date;              // e.g. "2026-05-03"
  const type = body.type;              // "clock_in" | "clock_out"
  const shiftId = body.shiftId || null;
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');

  const query = `
    INSERT INTO punches (employee_id, date, type, shift_id, idempotency_key, line_signature, payload_hash, created_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
    ON CONFLICT (idempotency_key) DO UPDATE
      SET line_signature = EXCLUDED.line_signature,
          payload_hash = EXCLUDED.payload_hash
      WHERE punches.idempotency_key = EXCLUDED.idempotency_key
    RETURNING *;
  `;

  const result = await pool.query(query, [employeeId, date, type, shiftId, idempotencyKey, lineSignature, payloadHash]);
  return result.rows[0];
}
```

### Apply migration

```bash
cd /opt/axentx/workio/server
psql workio < src/db/migrations/20260503_add_idempotency_and_constraints.sql
```

## 4. Verification

1. **Schema check**
   ```bash
   psql workio -c "\d punches"
   # Confirm idempotency_key, line_signature, payload_hash exist
   psql workio -c "\dCI+ punches"
   # Confirm uniq_punch_idempotency and idx_punch_employee_date_type_shift
   ```

2. **Idempotency test**
   - Send identical LINE webhook payload + signature twice (via curl or test script).
   - Verify only one row is created and second call returns the existing row without error.

3. **Constraint test**
   - Attempt to insert two rows with same (`employee_id`, `date`, `type`, `shift_id`) via direct SQL (with different idempotency keys).
   - Expect unique violation on `idx_punch_employee_date_type_shift`.

4. **Race-condition simulation**
   - Fire 10 concurrent requests with same idempotency key (use `Promise.all` in a test script).
   - Confirm exactly one row created and no deadlocks/errors.

5. **Quick smoke**
   - Start backend (`npm run dev`), trigger a real LINE punch, check DB row contains non-null `idempotency_key` and `line_signature`.
