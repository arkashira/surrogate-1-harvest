# workio / discovery

## Final consolidated solution

**Core problem**: duplicate active punches `(employee_id, punch_type, date)` caused by non-idempotent read-then-insert, webhook retries, and concurrent requests.  
**Goal**: enforce exactly-once semantics at the DB layer, provide traceability, and keep business rules enforceable and observable.

---

### 1. Schema changes (safe, additive)

File: `workio/server/src/db/schema.sql`

```sql
-- Idempotency/trace columns (nullable for backfill)
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS request_id UUID,
  ADD COLUMN IF NOT EXISTS client_msg_id TEXT;

-- Deterministic fingerprint for idempotency (indexed)
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS fingerprint TEXT;

-- One active punch per employee/date/type (soft-deleted rows ignored)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_unique_active
  ON punches (employee_id, date, punch_type)
  WHERE deleted_at IS NULL;

-- Fast idempotency lookups (optional but recommended)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_fingerprint
  ON punches (fingerprint)
  WHERE deleted_at IS NULL;
```

Notes:
- The partial unique index on `(employee_id, date, punch_type)` is the authoritative constraint for business correctness.
- `fingerprint` provides a stable, deterministic key for retries and idempotency checks.
- `request_id`/`client_msg_id` give traceability to webhooks and clients.

---

### 2. Deterministic fingerprint helper

File: `workio/server/src/lib/fingerprint.ts` (or inline)

```ts
import crypto from 'crypto';

export function buildPunchFingerprint(
  employee_id: string,
  punch_type: string,
  date: string,
  source = 'line'
): string {
  // Stable across retries; include only immutable fields
  return crypto
    .createHash('sha256')
    .update(`${employee_id}|${punch_type}|${date}|${source}`)
    .digest('hex');
}
```

---

### 3. Idempotent create-punch handler

File: `workio/server/src/routes/punch.ts` (or `services/punchService.ts`)

```ts
import { Request, Response } from 'express';
import { db } from '../db';
import { buildPunchFingerprint } from '../lib/fingerprint';

export async function createPunch(req: Request, res: Response) {
  const {
    employee_id,
    punch_type,
    date,
    lat,
    lng,
    location,
    source = 'line',
    request_id,
    client_msg_id,
  } = req.body;

  if (!employee_id || !punch_type || !date) {
    return res.status(400).json({ error: 'Missing required fields' });
  }

  const fingerprint = buildPunchFingerprint(employee_id, punch_type, date, source);
  const client = await db.connect();

  try {
    await client.query('BEGIN');

    // Try insert; if fingerprint exists, return existing row (idempotent)
    const upsert = await client.query(
      `INSERT INTO punches
         (employee_id, punch_type, date, lat, lng, location, source, fingerprint, request_id, client_msg_id, created_at)
       VALUES
         ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
       ON CONFLICT (fingerprint) DO NOTHING
       RETURNING *`,
      [
        employee_id,
        punch_type,
        date,
        lat,
        lng,
        location,
        source,
        fingerprint,
        request_id,
        client_msg_id,
      ]
    );

    // If insert occurred, return it
    if (upsert.rowCount === 1) {
      await client.query('COMMIT');
      return res.status(201).json({ punch: upsert.rows[0], duplicate: false });
    }

    // Otherwise, fetch existing active row (deterministic result)
    const existing = await client.query(
      `SELECT * FROM punches
       WHERE employee_id = $1 AND date = $2 AND punch_type = $3 AND deleted_at IS NULL
       LIMIT 1`,
      [employee_id, date, punch_type]
    );

    if (existing.rowCount === 0) {
      // Edge case: fingerprint exists but active row missing (e.g., hard-deleted or partial state)
      await client.query('ROLLBACK');
      return res.status(409).json({
        error: 'Idempotency conflict: fingerprint exists but no active punch found',
      });
    }

    await client.query('COMMIT');
    return res.status(200).json({ punch: existing.rows[0], duplicate: true });
  } catch (err: any) {
    await client.query('ROLLBACK');

    // Fallback for partial-index constraint violations
    if (err.code === '23505' || err.constraint) {
      const existing = await db.query(
        `SELECT * FROM punches
         WHERE employee_id = $1 AND date = $2 AND punch_type = $3 AND deleted_at IS NULL
         LIMIT 1`,
        [employee_id, date, punch_type]
      );
      if (existing.rowCount > 0) {
        return res.status(200).json({ punch: existing.rows[0], duplicate: true });
      }
    }

    console.error('Create punch error', err);
    return res.status(500).json({ error: 'Internal server error' });
  } finally {
    client.release();
  }
}
```

Key choices:
- Uses a transaction and explicit `fingerprint` conflict check first (fast, deterministic).
- Falls back to the partial unique index for safety if constraint violations occur.
- Returns the active row consistently so callers get the same result on retries.
- Rolls back on errors to avoid leaving partial state.

---

### 4. Client/webhook guidance

- **LINE webhook**: set `request_id` = webhook `deliveryId` (or equivalent). Compute fingerprint server-side using `source='line'`.
- **Mobile client**: generate a `client_msg_id` (UUID) per punch attempt and include it. Optionally include `request_id` if available.
- Both paths should send the same immutable fields (`employee_id`, `punch_type`, `date`, `source`) to ensure fingerprint stability.

---

### 5. Verification checklist

1. Apply schema changes to dev DB.
2. Start server: `npm run dev` in `workio/server`.
3. Create punch:
   ```bash
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -d '{"employee_id":1,"punch_type":"clock_in","date":"2026-05-03","lat":13.7,"lng":100.5,"source":"line","request_id":"req-123","client_msg_id":"cli-abc"}'
   ```
   → expect 201 with punch object.
4. Replay same payload → expect 200 with same punch and `duplicate: true`.
5. Concurrent test: run two requests in parallel (same payload) → expect only one row created.
6. Confirm indexes exist:
   ```sql
   SELECT indexname FROM pg_indexes WHERE tablename = 'punches' AND indexname IN ('idx_punches_unique_active','idx_punches_fingerprint');
   ```
7. Confirm idempotency/trace columns populated:
   ```sql
   SELECT employee_id, date, punch_type, request_id, client_msg_id, fingerprint FROM punches WHERE employee_id = 1 AND date = '2026-05-03';
   ```

---

### 6. Why this resolves contradictions

- **Correctness**: the partial unique index on `(employee_id, date, punch_type)` is the source of truth for business rules; `fingerprint` is an implementation detail for idempotency.
- **Actionability**: concrete schema changes, a clear handler with transaction + upsert, and verification steps that can be run immediately.
- **Traceability**: `request_id`/`client_msg_id` + `fingerprint` let you correlate retries to original events and debug duplicates.
