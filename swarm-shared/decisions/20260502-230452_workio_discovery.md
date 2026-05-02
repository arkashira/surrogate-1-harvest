# workio / discovery

## Final synthesized solution (correct + actionable)

### 1) Root causes (agreed)

- No DB-level uniqueness for `(employee_id, punch_type, punch_date)` → duplicates possible.
- No atomic upsert / idempotency handling → retries (mobile/LINE webhook) create double punches.
- Check-then-insert race condition under concurrency.
- No durable idempotency tracking to make retries safe.

### 2) Schema changes (run once)

```sql
-- 1) Unique punch per employee/type/date (only for non-deleted rows)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_employee_type_date
  ON punches (employee_id, punch_type, punch_date)
  WHERE deleted_at IS NULL;

-- 2) Idempotency table (lightweight, separate from punches)
CREATE TABLE IF NOT EXISTS punch_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  employee_id     INTEGER NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Optional helper index (if you keep idempotency_key on punches instead)
-- ALTER TABLE punches ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency_key
--   ON punches (idempotency_key) WHERE idempotency_key IS NOT NULL;
```

Use the **separate idempotency table** (recommended) because:
- Keeps punches schema stable.
- Guarantees one request = one idempotency record regardless of punch uniqueness rules.
- Avoids NULL/partial uniqueness edge cases on punches.

### 3) Endpoint implementation (atomic, transactional)

```ts
// server/src/routes/punch.ts
import { db } from '../db';
import express from 'express';
const router = express.Router();

router.post('/punch', async (req, res) => {
  const { employee_id, punch_type, punch_date, latitude, longitude } = req.body;
  const idempotencyKey = req.header('X-Idempotency-Key');

  if (!idempotencyKey) {
    return res.status(400).json({ error: 'X-Idempotency-Key header required' });
  }

  const client = await db.pool.connect();
  try {
    await client.query('BEGIN');

    // 1) Claim idempotency key (atomic)
    const claim = await client.query(
      `INSERT INTO punch_idempotency (idempotency_key, employee_id)
       VALUES ($1, $2)
       ON CONFLICT (idempotency_key) DO NOTHING
       RETURNING idempotency_key`,
      [idempotencyKey, employee_id]
    );

    if (claim.rowCount === 0) {
      // Key exists: fetch existing punch and return it (idempotent response)
      const existing = await client.query(
        `SELECT * FROM punches
         WHERE employee_id = $1 AND punch_type = $2 AND punch_date = $3
           AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1`,
        [employee_id, punch_type, punch_date]
      );
      await client.query('COMMIT');
      return res.json({ duplicate: true, punch: existing.rows[0] || null });
    }

    // 2) Try to create punch (atomic; unique index prevents duplicates)
    const insert = await client.query(
      `INSERT INTO punches (employee_id, punch_type, punch_date, latitude, longitude)
       VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (employee_id, punch_type, punch_date)
       DO UPDATE SET updated_at = NOW()
       WHERE punches.deleted_at IS NULL
       RETURNING *`,
      [employee_id, punch_type, punch_date, latitude, longitude]
    );

    await client.query('COMMIT');

    // If constraint blocked insert (race), fetch existing
    if (insert.rowCount === 0) {
      const existing = await client.query(
        `SELECT * FROM punches
         WHERE employee_id = $1 AND punch_type = $2 AND punch_date = $3
           AND deleted_at IS NULL
         ORDER BY created_at DESC LIMIT 1`,
        [employee_id, punch_type, punch_date]
      );
      return res.json({ duplicate: true, punch: existing.rows[0] || null });
    }

    return res.json({ duplicate: false, punch: insert.rows[0] });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Punch error:', err);
    return res.status(500).json({ error: 'Internal server error' });
  } finally {
    client.release();
  }
});
```

### 4) Why this is correct and safe

- **Idempotency table + primary key** ensures each client retry with the same key is detected atomically.
- **Unique index on punches** prevents duplicates even if two requests use different keys (DB is the final safeguard).
- **Single transaction** makes claim + insert atomic; no check-then-insert race.
- **ON CONFLICT DO NOTHING/UPDATE** keeps behavior deterministic under concurrency.
- **deleted_at filter** ensures soft-deleted rows don’t block new punches.

### 5) Verification checklist

1. Apply schema SQL; confirm:
   - `idx_punches_employee_type_date` exists.
   - `punch_idempotency` table exists.
2. First request with new `X-Idempotency-Key` → `duplicate: false`, one punch row.
3. Repeat same request → `duplicate: true`, same punch row, no new punch.
4. Different idempotency key for same `(employee_id, punch_type, punch_date)` → `duplicate: true`, no new punch (unique index blocks).
5. Concurrent requests (different keys) for same punch → exactly one succeeds; others return existing.
6. Soft-deleted punch (if applicable) should not block a new punch for same day/type.

This solution is minimal, DB-enforced, and safe for mobile retries and concurrent requests.
