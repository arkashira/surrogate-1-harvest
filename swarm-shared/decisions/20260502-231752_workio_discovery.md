# workio / discovery

## 1. Diagnosis

- No idempotency key on LINE webhook payloads → redeliveries create duplicate punch rows.
- Punch creation uses read-then-insert (non-atomic) → races between concurrent/redelivered webhooks can create two active punches for same employee/date.
- No storage-level uniqueness constraint for active punches → `(employee_id, punch_type, date, status='active')` duplicates possible.
- Missing traceability (request-id / webhook-id) on punch rows → hard to detect/repair duplicates or correlate logs.
- No upsert path for clock-in/out → retry-safe endpoint absent; clients must handle duplicates manually.

## 2. Proposed change

File: `workio/server/src/db/schema.sql`  
Scope: add idempotency + uniqueness guard and traceability columns to `punches` table, plus an upsert helper function.  
Secondary: `workio/server/src/services/punchService.ts` — implement idempotent `clockIn/clockOut` using the DB constraint.

## 3. Implementation

### 3.1 Schema change (idempotency + uniqueness)

```sql
-- workio/server/src/db/schema.sql
-- Add idempotency key and traceability; enforce one active punch per employee per date per punch_type
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS line_webhook_id VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS trace_id VARCHAR(255) NULL;

-- Index for fast idempotency checks
CREATE INDEX IF NOT EXISTS idx_punches_idempotency_key ON punches(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_punches_line_webhook_id ON punches(line_webhook_id) WHERE line_webhook_id IS NOT NULL;

-- Prevent duplicate active punches for same employee/date/punch_type
-- Allow multiple historical/closed punches (status != 'active') but only one active
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_active_per_day_type
  ON punches(employee_id, punch_date, punch_type)
  WHERE status = 'active';
```

### 3.2 Upsert helper (SQL)

```sql
-- Optional helper: safe upsert for active punch by idempotency key
-- Returns the punch row (existing or created)
CREATE OR REPLACE FUNCTION upsert_punch_by_idempotency(
  p_employee_id INTEGER,
  p_punch_type VARCHAR(10),
  p_punch_date DATE,
  p_clock_in TIMESTAMP WITH TIME ZONE,
  p_clock_out TIMESTAMP WITH TIME ZONE,
  p_status VARCHAR(20),
  p_idempotency_key VARCHAR,
  p_line_webhook_id VARCHAR DEFAULT NULL,
  p_trace_id VARCHAR DEFAULT NULL,
  p_location VARCHAR DEFAULT NULL,
  p_notes TEXT DEFAULT NULL
)
RETURNS TABLE (
  id INTEGER,
  employee_id INTEGER,
  punch_type VARCHAR,
  punch_date DATE,
  clock_in TIMESTAMP WITH TIME ZONE,
  clock_out TIMESTAMP WITH TIME ZONE,
  status VARCHAR,
  location VARCHAR,
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE,
  updated_at TIMESTAMP WITH TIME ZONE,
  idempotency_key VARCHAR,
  line_webhook_id VARCHAR,
  trace_id VARCHAR
) AS $$
BEGIN
  -- Try to find existing by idempotency key first (fastest)
  RETURN QUERY
  UPDATE punches
  SET
    clock_in = COALESCE(p_clock_in, clock_in),
    clock_out = COALESCE(p_clock_out, clock_out),
    status = p_status,
    location = COALESCE(p_location, location),
    notes = COALESCE(p_notes, notes),
    updated_at = NOW()
  WHERE idempotency_key = p_idempotency_key
  RETURNING *;

  IF FOUND THEN RETURN; END IF;

  -- If not found, try insert (will fail on unique active constraint if race)
  BEGIN
    RETURN QUERY
    INSERT INTO punches (
      employee_id, punch_type, punch_date, clock_in, clock_out, status,
      location, notes, idempotency_key, line_webhook_id, trace_id
    ) VALUES (
      p_employee_id, p_punch_type, p_punch_date, p_clock_in, p_clock_out, p_status,
      p_location, p_notes, p_idempotency_key, p_line_webhook_id, p_trace_id
    )
    RETURNING *;
  EXCEPTION WHEN unique_violation THEN
    -- Race lost: select the row that won (by idempotency or unique active constraint)
    RETURN QUERY
    SELECT * FROM punches
    WHERE idempotency_key = p_idempotency_key
       OR (employee_id = p_employee_id AND punch_date = p_punch_date AND punch_type = p_punch_type AND status = 'active')
    LIMIT 1;
  END;
END;
$$ LANGUAGE plpgsql;
```

### 3.3 Service change (idempotent clock-in/out)

File: `workio/server/src/services/punchService.ts`

```ts
// workio/server/src/services/punchService.ts
import { pool } from './db';

export interface ClockInOutParams {
  employeeId: number;
  punchType: 'in' | 'out';
  punchDate: string; // YYYY-MM-DD
  clockTime: Date;
  location?: string;
  lineWebhookId?: string;
  traceId?: string;
  notes?: string;
}

/**
 * Idempotent clock-in/out.
 * Uses idempotency_key derived from caller (e.g., lineWebhookId + employeeId + punchType + date).
 * Returns existing active punch if already present (idempotent).
 */
export async function clockInOutIdempotent(params: ClockInOutParams) {
  const {
    employeeId,
    punchType,
    punchDate,
    clockTime,
    location,
    lineWebhookId,
    traceId,
    notes,
  } = params;

  // Deterministic idempotency key: prefer provided lineWebhookId, else compose
  const idempotencyKey = lineWebhookId
    ? `line:${lineWebhookId}:${employeeId}:${punchType}:${punchDate}`
    : `custom:${traceId || 'none'}:${employeeId}:${punchType}:${punchDate}:${clockTime.toISOString()}`;

  const punchTimestamp = clockTime;

  // Use upsert function for atomicity and race safety
  const result = await pool.query(
    `SELECT * FROM upsert_punch_by_idempotency(
       $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
     )`,
    [
      employeeId,
      punchType,
      punchDate,
      punchType === 'in' ? punchTimestamp : null,
      punchType === 'out' ? punchTimestamp : null,
      'active',
      idempotencyKey,
      lineWebhookId || null,
      traceId || null,
      location || null,
      notes || null,
    ]
  );

  return result.rows[0];
}
```

### 3.4 Webhook usage (example)

In your LINE webhook handler:

```ts
const lineWebhookId = body.events[0]?.webhookEventId; // or use delivery id / X-Line-Signature-derived hash
const employeeId = await resolveEmployeeIdByLineUserId(userId);
const punch = await clockInOutIdempotent({
  employeeId,
  punchType: shouldClockIn ? 'in' : 'out',
  punchDate: todayYYYYMMDD,
  clockTime: new Date(),
  location: gps,
  lineWebhookId,
  traceId: generateTraceId(),
});
```

## 4. Verification

1. Apply schema migration:
   ```bash
   psql workio < server/src/db/schema.sql
   ```
   Confirm indexes exist:
   ```sql
   SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'punches';
   ```

2. Idempotency test (single webhook redelivery):
   - Send same LINE webhook payload twice (same `webhookEventId`).
   - Verify only one punch row created and `idempotency_key` matches.
   - Query:
     ```sql
     SELECT idempotency_key, COUNT(*) FROM punches WHERE id
