# workio / discovery

## 1) Diagnosis

- No idempotency enforcement on `/punches` write path → repeated LINE webhook deliveries or frontend retries create duplicate punch records.
- Missing DB uniqueness constraint for “one open punch per user” → allows multiple concurrent clock-ins or overlapping sessions.
- No server-side validation that a user cannot clock-in while already clocked-in → business rule enforced only (or not) in UI.
- Punch records lack a deterministic idempotency key (e.g., `line_webhook_delivery_id` or client-generated `request_id`) → retries cannot be de-duplicated safely.
- No audit or de-duplication layer before insert → data quality degrades under network retries and LINE at-least-once delivery.

## 2) Proposed change

File: `/opt/axentx/workio/server/src/routes/punches.ts` (or equivalent route handling punch creation)  
Scope: add idempotency key extraction, uniqueness check, and DB constraint for `(user_id, clock_out_time IS NULL)` to enforce one open punch per user.

## 3) Implementation

```bash
# 1) Add DB constraint (run once)
psql workio <<'SQL'
-- Prevent multiple open punches per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch_per_user
ON punches (user_id)
WHERE clock_out_time IS NULL;

-- Optional: add idempotency column if not present
ALTER TABLE punches
ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency
ON punches (idempotency_key)
WHERE idempotency_key IS NOT NULL;
SQL
```

```typescript
// 2) server/src/routes/punches.ts (or equivalent)
import { Request, Response } from 'express';
import { pool } from '../db';

export async function createPunch(req: Request, res: Response) {
  const { user_id, type, latitude, longitude } = req.body;
  const idempotencyKey = req.headers['x-idempotency-key'] || (req as any).lineWebhookDeliveryId || null;

  if (!user_id || !type) {
    return res.status(400).json({ error: 'user_id and type required' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check
    if (idempotencyKey) {
      const idem = await client.query(
        'SELECT id FROM punches WHERE idempotency_key = $1',
        [idempotencyKey]
      );
      if (idem.rows.length > 0) {
        await client.query('ROLLBACK');
        return res.status(200).json({ ok: true, message: 'duplicate ignored', punch: idem.rows[0] });
      }
    }

    // Business rule: one open punch per user
    if (type === 'in') {
      const open = await client.query(
        'SELECT id FROM punches WHERE user_id = $1 AND clock_out_time IS NULL',
        [user_id]
      );
      if (open.rows.length > 0) {
        await client.query('ROLLBACK');
        return res.status(409).json({ error: 'already clocked in', punch: open.rows[0] });
      }
    }

    // Insert
    const result = await client.query(
      `INSERT INTO punches (user_id, type, latitude, longitude, clock_in_time, idempotency_key)
       VALUES ($1, $2, $3, $4, NOW(), $5)
       RETURNING *`,
      [user_id, type, latitude, longitude, idempotencyKey]
    );

    await client.query('COMMIT');
    return res.status(201).json({ ok: true, punch: result.rows[0] });
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Unique violation from constraint -> treat as conflict
    if (err.code === '23505') {
      return res.status(409).json({ error: 'duplicate or already clocked in' });
    }
    console.error(err);
    return res.status(500).json({ error: 'internal error' });
  } finally {
    client.release();
  }
}
```

If using a different table/column naming (e.g., `punch_records` or `clock_in_time`/`clock_out_time`), adjust SQL/columns accordingly.

## 4) Verification

1. Apply DB migration:
   ```bash
   psql workio -c "SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'idx_one_open_punch_per_user';"
   # Should return 1
   ```

2. Start backend:
   ```bash
   cd /opt/axentx/workio/server
   npm run dev
   ```

3. Test duplicate prevention:
   ```bash
   # First clock-in
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -d '{"user_id":1,"type":"in","latitude":13.7,"longitude":100.5}'

   # Second clock-in for same user -> expect 409
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -d '{"user_id":1,"type":"in","latitude":13.7,"longitude":100.5}'

   # Same request with idempotency key -> expect 200 duplicate ignored
   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -H "x-idempotency-key: abc123" \
     -d '{"user_id":2,"type":"in","latitude":13.7,"longitude":100.5}'

   curl -X POST http://localhost:3000/punches \
     -H "Content-Type: application/json" \
     -H "x-idempotency-key: abc123" \
     -d '{"user_id":2,"type":"in","latitude":13.7,"longitude":100.5}'
   ```

4. Confirm LINE webhook retry safety: replay a recorded webhook payload with same delivery ID twice; second request should return 200 with `duplicate ignored` and no new row.
