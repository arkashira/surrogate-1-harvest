# workio / discovery

## Final consolidated solution (best of both)

**Core principle**: enforce uniqueness at the database (source of truth), make writes idempotent at the API, and provide fast, race-safe lookups. Favor correctness and concrete actionability.

---

### 1. Diagnosis (resolved)

- **Missing storage-level uniqueness** → duplicates possible at DB layer.  
  **Fix**: add a real `UNIQUE` constraint on `(employee_id, punch_type, date)`.

- **No idempotency in API contract** → retries (client or LINE webhook) create duplicates.  
  **Fix**: accept `Idempotency-Key`, perform transactional upsert, and return stable IDs on replay.

- **Race conditions on concurrent writes** → app-layer checks alone are racy.  
  **Fix**: rely on `ON CONFLICT` at DB + short transaction; use DB constraint as final guard.

- **Missing fast conflict-check index** → duplicate checks and reporting are slower and less safe under load.  
  **Fix**: add composite index aligned with constraint and common query patterns.

- **Ambiguity in upsert behavior** → earlier candidate updated existing row on conflict (overwrites timestamp/location).  
  **Fix**: **do not overwrite** an existing punch by default; only insert if absent. This prevents accidental loss of original punch data and keeps behavior predictable. Allow explicit updates via a separate PATCH/PUT endpoint if business needs require corrections.

---

### 2. Schema changes (single source of truth)

File: `workio/server/src/db/schema.sql`

```sql
-- Ensure created_at default exists for audit/ordering
ALTER TABLE punches
  ALTER COLUMN created_at SET DEFAULT NOW();

-- Enforce one punch per (employee, punch_type, date) at storage level
-- This is the last line of defense against duplicates.
ALTER TABLE punches
  ADD CONSTRAINT punches_employee_type_date_uniq
  UNIQUE (employee_id, punch_type, date);

-- Fast conflict check and common lookup pattern (employee + date + type)
CREATE INDEX IF NOT EXISTS idx_punches_employee_date_type
  ON punches (employee_id, date, punch_type);

-- Optional but recommended: fast time-ordered reads / recent punches
CREATE INDEX IF NOT EXISTS idx_punches_created_at_desc
  ON punches (created_at DESC);
```

**Why this combination**:
- The constraint guarantees correctness.
- The index `(employee_id, date, punch_type)` speeds up both conflict checks and day-based queries.
- `created_at` index helps listing/reports without affecting write safety.

---

### 3. API implementation (idempotent, race-safe)

File: `workio/server/src/routes/punches.ts`

```ts
import { Request, Response, Router } from 'express';
import { pool } from '../db';

const router = Router();

/**
 * POST /punches
 * Body: { employee_id, punch_type, date, timestamp, latitude?, longitude? }
 * Headers: Idempotency-Key (recommended: client-generated UUID)
 *
 * Behavior:
 * - If Idempotency-Key provided and a punch already exists for that
 *   (employee_id, punch_type, date), return existing record (200, created: false).
 * - Otherwise, attempt insert. On conflict (race or duplicate), return existing (200).
 * - Never overwrite an existing punch's timestamp/location by default.
 */
router.post('/', async (req: Request, res: Response) => {
  const { employee_id, punch_type, date, timestamp, latitude, longitude } = req.body;
  const idempotencyKey = req.header('Idempotency-Key');

  if (!employee_id || !punch_type || !date || !timestamp) {
    return res.status(400).json({ error: 'Missing required fields' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // 1) Fast path: if idempotency key present, try to find existing punch
    if (idempotencyKey) {
      const existing = await client.query(
        `SELECT id, created_at, updated_at FROM punches
         WHERE employee_id = $1 AND punch_type = $2 AND date = $3`,
        [employee_id, punch_type, date]
      );
      if (existing.rows.length > 0) {
        await client.query('COMMIT');
        return res.status(200).json({
          id: existing.rows[0].id,
          created: false,
          created_at: existing.rows[0].created_at,
          updated_at: existing.rows[0].updated_at,
          note: 'duplicate suppressed'
        });
      }
    }

    // 2) Insert; on conflict return existing without overwriting
    const result = await client.query(
      `INSERT INTO punches (employee_id, punch_type, date, timestamp, latitude, longitude, created_at, updated_at)
       VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
       ON CONFLICT (employee_id, punch_type, date) DO NOTHING
       RETURNING id, created_at, updated_at`,
      [employee_id, punch_type, date, timestamp, latitude, longitude]
    );

    if (result.rowCount && result.rowCount > 0) {
      await client.query('COMMIT');
      return res.status(201).json({
        id: result.rows[0].id,
        created: true,
        created_at: result.rows[0].created_at,
        updated_at: result.rows[0].updated_at
      });
    }

    // 3) Conflict occurred (DO NOTHING) — fetch existing row
    const existing = await client.query(
      `SELECT id, created_at, updated_at FROM punches
       WHERE employee_id = $1 AND punch_type = $2 AND date = $3`,
      [employee_id, punch_type, date]
    );
    await client.query('COMMIT');
    return res.status(200).json({
      id: existing.rows[0].id,
      created: false,
      created_at: existing.rows[0].created_at,
      updated_at: existing.rows[0].updated_at,
      note: 'duplicate'
    });
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Defensive: handle unique violation if it slips through
    if (err.code === '23505') {
      const existing = await pool.query(
        `SELECT id, created_at, updated_at FROM punches
         WHERE employee_id = $1 AND punch_type = $2 AND date = $3`,
        [employee_id, punch_type, date]
      );
      return res.status(200).json({
        id: existing.rows[0]?.id,
        created: false,
        created_at: existing.rows[0]?.created_at,
        updated_at: existing.rows[0]?.updated_at,
        note: 'duplicate (constraint fallback)'
      });
    }
    console.error('Punch write error', err);
    return res.status(500).json({ error: 'Internal server error' });
  } finally {
    client.release();
  }
});

export default router;
```

**Key choices**:
- Use `ON CONFLICT DO NOTHING` + fetch existing row: avoids accidental overwrites and keeps behavior predictable.
- Idempotency key is optional but recommended; when provided, we short-circuit quickly.
- All conflict resolution happens inside a transaction to avoid races.
- Return stable `id` and timestamps so clients can distinguish new vs. duplicate.

---

### 4. Client / webhook guidance (actionable)

- Generate a short `Idempotency-Key` (UUID v4) per punch attempt on client/webhook.
- Retry with the same key on network errors; server will dedupe and return the same `id`.
- For LINE webhook handlers, optionally keep a small in-memory/Redis set of recent keys (e.g., last 5–10 minutes) to reduce DB roundtrips for immediate duplicates.
- Never reuse an `Idempotency-Key` for a logically different punch (e.g., different `date` or `punch_type`).

---

### 5. Verification (concrete steps)

1. **Apply schema**
   ```bash
   psql workio < server/src/db/schema.sql
   ```
   Confirm:
   ```sql
   \d punches
   -- should show UNIQUE CONSTRAINT punches_employee_type_date_uniq
   -- and indexes idx_punches_employee_date_type and idx_punches_created_at_desc
   ```

2. **Basic duplicate suppression**
  
