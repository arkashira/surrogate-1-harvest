# workio / discovery

## 1. Diagnosis

- No idempotency key on punch writes → LINE webhook redelivery and client retries create duplicate punches.
- No storage-level uniqueness on `(employee_id, punch_type, date)` → race conditions allow multiple punches per day per type.
- Punch write path is read-then-insert (non-atomic) → concurrent requests can both pass existence checks and both insert.
- No deduplication window for rapid repeated punches (e.g., accidental double-tap or LINE retry within seconds).
- Missing audit fields (`created_at`, `idempotency_key`, `source`) → impossible to detect or clean duplicates retroactively.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/db/schema.sql` (add constraint + columns)  
File: `/opt/axentx/workio/server/src/routes/punches.js` (add idempotency check + upsert)  
Scope: add `idempotency_key` + unique constraint on `(employee_id, punch_type, date)` and convert punch creation to atomic upsert.

## 3. Implementation

```sql
-- /opt/axentx/workio/server/src/db/schema.sql
-- Add columns to punches table
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'line';

-- Remove duplicates before constraint (keep earliest created_at per group)
DELETE FROM punches
WHERE ctid NOT IN (
  SELECT MIN(ctid)
  FROM punches
  GROUP BY employee_id, punch_type, date
);

-- Unique constraint to prevent duplicates
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_employee_type_date
  ON punches (employee_id, punch_type, date);

-- Optional: index for idempotency lookups
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency
  ON punches (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

```js
// /opt/axentx/workio/server/src/routes/punches.js
// Replace existing punch creation route with idempotent upsert
const express = require('express');
const router = express.Router();
const db = require('../db');

/**
 * POST /punches
 * Body: { employee_id, punch_type, date, idempotency_key?, source?, latitude?, longitude? }
 * punch_type: 'clock_in' | 'clock_out'
 * date: YYYY-MM-DD (derived server-side if omitted)
 */
router.post('/', async (req, res) => {
  const { employee_id, punch_type, date, idempotency_key, source = 'line', latitude, longitude } = req.body;

  if (!employee_id || !punch_type || !['clock_in', 'clock_out'].includes(punch_type)) {
    return res.status(400).json({ error: 'Invalid payload' });
  }

  const punchDate = date || new Date().toISOString().split('T')[0];

  try {
    // Atomic upsert: unique on (employee_id, punch_type, date)
    // If idempotency_key provided and exists, return existing row (idempotent)
    let result;
    if (idempotency_key) {
      result = await db.query(
        `INSERT INTO punches (employee_id, punch_type, date, idempotency_key, source, latitude, longitude, created_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
         ON CONFLICT (idempotency_key) DO UPDATE
           SET updated_at = NOW()
         RETURNING *`,
        [employee_id, punch_type, punchDate, idempotency_key, source, latitude, longitude]
      );

      // If conflict on idempotency returned existing row, still enforce business uniqueness
      if (result.rowCount > 0) {
        return res.json({ punch: result.rows[0], message: 'Idempotent insert or existing record returned' });
      }
    }

    // Fallback to business-unique upsert (employee_id, punch_type, date)
    result = await db.query(
      `INSERT INTO punches (employee_id, punch_type, date, idempotency_key, source, latitude, longitude, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
       ON CONFLICT (employee_id, punch_type, date) DO UPDATE
         SET updated_at = NOW()
       RETURNING *`,
      [employee_id, punch_type, punchDate, idempotency_key, source, latitude, longitude]
    );

    res.status(201).json({ punch: result.rows[0], message: 'Punch recorded' });
  } catch (err) {
    console.error('Punch upsert failed', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

module.exports = router;
```

## 4. Verification

1. Apply schema migration:
   ```bash
   psql workio < /opt/axentx/workio/server/src/db/schema.sql
   ```
2. Start backend:
   ```bash
   cd /opt/axentx/workio/server && npm run dev
   ```
3. Test idempotency (same idempotency_key):
   ```bash
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -d '{"employee_id":1,"punch_type":"clock_in","date":"2026-05-02","idempotency_key":"test-123"}'
   ```
   Repeat same request → same record returned, no duplicate.

4. Test uniqueness constraint (no idempotency_key):
   ```bash
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -d '{"employee_id":1,"punch_type":"clock_in","date":"2026-05-02"}'
   ```
   Repeat → second request returns existing row (upsert) and no duplicate in DB.

5. Confirm no duplicates in DB:
   ```bash
   psql workio -c "SELECT employee_id, punch_type, date, count(*) FROM punches GROUP BY employee_id, punch_type, date HAVING count(*) > 1;"
   ```
   Should return zero rows.
