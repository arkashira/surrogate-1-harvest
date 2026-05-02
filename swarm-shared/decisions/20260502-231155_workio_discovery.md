# workio / discovery

## Final consolidated solution

**Core problem**: duplicate punches on retries (LINE webhook + client) because there is no storage-level uniqueness and no idempotency.  
**Goal**: enforce exactly-once punch creation per `(employee_id, punch_type, date)` with safe retries and concrete, actionable steps.

---

### 1. Schema changes (idempotent migration)

File: `workio/server/src/db/schema.sql`

```sql
-- Add idempotency and audit columns if missing
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'line';

-- Remove existing duplicates safely: keep earliest per employee/punch_type/date
DELETE FROM punches
WHERE ctid NOT IN (
  SELECT MIN(ctid)
  FROM punches
  GROUP BY employee_id, punch_type, DATE(created_at)
);

-- Unique constraint for one active punch per employee/punch_type/date
-- Covers clock_in/clock_out on same day; allows multiple historical records if needed
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_unique_active_day
ON punches (employee_id, punch_type, DATE(created_at))
WHERE punch_type IN ('clock_in', 'clock_out');

-- Fast idempotency lookup
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency
ON punches (idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

Apply migration:

```bash
cd /opt/axentx/workio
sudo -u postgres psql workio -f server/src/db/schema.sql
```

---

### 2. Punch write path (idempotent upsert)

File: `workio/server/src/routes/punch.ts` (or `.js` equivalent)

```ts
import { Request, Response } from 'express';
import { pool } from '../db';

export async function createPunch(req: Request, res: Response) {
  const { employee_id, punch_type, latitude, longitude, idempotency_key } = req.body;

  if (!employee_id || !punch_type || !idempotency_key) {
    return res.status(400).json({ error: 'employee_id, punch_type, and idempotency_key are required' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency fast path
    const existingIdempotency = await client.query(
      'SELECT id FROM punches WHERE idempotency_key = $1',
      [idempotency_key]
    );
    if (existingIdempotency.rows.length > 0) {
      await client.query('COMMIT');
      return res.status(200).json({ ok: true, duplicate: true, id: existingIdempotency.rows[0].id });
    }

    // Deterministic date for constraint (use provided date or today)
    const day = (req.body.date || new Date().toISOString().split('T')[0]);

    // Insert with idempotency; let unique index block duplicates
    const result = await client.query(
      `INSERT INTO punches (employee_id, punch_type, latitude, longitude, idempotency_key, source, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, NOW())
       RETURNING *`,
      [employee_id, punch_type, latitude, longitude, idempotency_key, req.body.source || 'line']
    );

    await client.query('COMMIT');
    return res.status(201).json({ ok: true, duplicate: false, punch: result.rows[0] });
  } catch (err: any) {
    await client.query('ROLLBACK');

    // Unique constraint violation: return existing punch for this employee/type/date
    if (err.code === '23505' && err.constraint === 'idx_punches_unique_active_day') {
      const existing = await pool.query(
        `SELECT * FROM punches
         WHERE employee_id = $1 AND punch_type = $2 AND DATE(created_at) = $3
         ORDER BY created_at ASC LIMIT 1`,
        [employee_id, punch_type, day]
      );
      if (existing.rows.length > 0) {
        return res.status(200).json({ ok: true, duplicate: true, punch: existing.rows[0] });
      }
    }

    console.error('Punch create error', err);
    return res.status(500).json({ error: 'Internal server error' });
  } finally {
    client.release();
  }
}
```

Key points:
- Require `idempotency_key` on all requests.
- Use a transaction with explicit `BEGIN`/`COMMIT`/`ROLLBACK` to avoid race windows.
- Fast idempotency check first; then rely on the unique index for correctness.
- On constraint violation, return the earliest existing punch for that employee/type/date.

---

### 3. Client/webhook idempotency key generation

- **LINE webhook**: deterministic key, e.g. `line:{userId}:{eventType}:{eventTimestamp}` or SHA-256 hash of the stable event payload.
- **API clients**: require `idempotency_key` in body (or header) and reject if missing.
- Keep keys reasonably bounded (e.g., 24–48 hour TTL for cleanup if you add housekeeping later).

---

### 4. Verification checklist

1. Apply schema migration and confirm indexes exist:
   ```sql
   SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'punches';
   ```
   - Expect `idx_punches_unique_active_day` and `idx_punches_idempotency`.

2. Start backend:
   ```bash
   cd workio/server && npm run dev
   ```

3. Send duplicate payloads with same `idempotency_key`:
   ```bash
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -d '{"employee_id":1,"punch_type":"clock_in","latitude":13.75,"longitude":100.55,"idempotency_key":"test-dup-key"}'
   ```
   - First call: 201 with punch.
   - Second call: 200 with `"duplicate":true` and same punch.

4. Attempt conflicting punch for same `(employee_id, punch_type, date)` with a different `idempotency_key`:
   - Should be rejected by the unique index and handler returns the existing punch.

5. Confirm no duplicate rows:
   ```sql
   SELECT employee_id, punch_type, DATE(created_at), COUNT(*)
   FROM punches
   WHERE punch_type IN ('clock_in','clock_out')
   GROUP BY employee_id, punch_type, DATE(created_at)
   HAVING COUNT(*) > 1;
   ```
   - Expect zero rows.

---

### 5. Why this resolves contradictions and is actionable

- Uses **both** idempotency key (fast, user-friendly retries) **and** a storage-level unique index (source of truth) — avoids races and guarantees correctness.
- Explicit transaction prevents check-then-insert race windows.
- Clear, copy-paste schema migration and route implementation with minimal, focused changes.
- Concrete verification steps to confirm behavior and index presence.
- Client guidance for deterministic idempotency keys ensures retries are safe across LINE and API callers.
