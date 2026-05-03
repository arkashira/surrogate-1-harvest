# workio / discovery

## 1. Diagnosis

- No idempotency key on `/webhook/line` POST → duplicate punches when LINE retries (network blips, 5xx, slow client).
- Punch write path allows multiple “open” punches per user (no partial unique index) → corrupt state if race or retry.
- No transactional upsert for idempotency → concurrent webhook deliveries can insert duplicate punches before constraint check commits.
- Missing observability on webhook processing (no request-id, no idempotency-key trace) → hard to debug duplicates in prod.
- No replay-safe ingestion path for backfilled/repaired events (no idempotency table) → manual cleanup required.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (or equivalent route handler).  
Scope: add idempotency table + partial unique index + transactional upsert for punches; expose idempotency key via `X-Idempotency-Key` (LINE message id) and return 200 for repeats.

## 3. Implementation

```sql
-- server/src/db/schema.sql
-- Idempotency table (lightweight, request-level)
CREATE TABLE IF NOT EXISTS webhook_idempotency (
  idempotency_key TEXT NOT NULL PRIMARY KEY,
  tenant_id       INTEGER NOT NULL,
  user_id         INTEGER NOT NULL,
  punch_id        INTEGER NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- One open punch per user (tenant-scoped)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch_per_user
ON punches (tenant_id, user_id)
WHERE clock_out_at IS NULL;
```

```ts
// server/src/routes/webhook/line.ts
import { Router, Request, Response } from 'express';
import { pool } from '../../db';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

router.post('/line', async (req: Request, res: Response) => {
  const client = await pool.connect();
  const idempotencyKey = req.header('X-Idempotency-Key') || req.body.events?.[0]?.message?.id;
  if (!idempotencyKey) return res.status(400).json({ error: 'missing_idempotency_key' });

  const { source, message, timestamp } = req.body.events?.[0] || {};
  if (!source?.userId || !message?.text) return res.status(400).json({ error: 'invalid_event' });

  const lineUserId = source.userId;
  const text = message.text.trim().toLowerCase();
  const isClockIn = text === 'in' || text === 'clock in';
  const isClockOut = text === 'out' || text === 'clock out';
  if (!isClockIn && !isClockOut) return res.status(400).json({ error: 'unknown_command' });

  try {
    await client.query('BEGIN');

    // Resolve tenant & user (simplified; adapt to your schema)
    const userRes = await client.query(
      `SELECT id, tenant_id FROM users WHERE line_user_id = $1 LIMIT 1`,
      [lineUserId]
    );
    if (userRes.rowCount === 0) {
      await client.query('ROLLBACK');
      return res.status(404).json({ error: 'user_not_found' });
    }
    const { id: userId, tenant_id: tenantId } = userRes.rows[0];

    // Idempotency check
    const idemRes = await client.query(
      `SELECT punch_id FROM webhook_idempotency WHERE idempotency_key = $1`,
      [idempotencyKey]
    );
    if (idemRes.rowCount > 0) {
      await client.query('COMMIT');
      return res.status(200).json({ ok: true, replayed: true, punch_id: idemRes.rows[0].punch_id });
    }

    let punchId: number;
    if (isClockIn) {
      // Try insert new open punch; unique index prevents multiple open punches
      const insertRes = await client.query(
        `INSERT INTO punches (tenant_id, user_id, clock_in_at, clock_in_location, created_at)
         VALUES ($1, $2, NOW(), $3, NOW())
         RETURNING id`,
        [tenantId, userId, req.body.location || null]
      );
      punchId = insertRes.rows[0].id;
    } else {
      // Clock out latest open punch
      const updateRes = await client.query(
        `UPDATE punches
         SET clock_out_at = NOW(), clock_out_location = $1, updated_at = NOW()
         WHERE tenant_id = $2 AND user_id = $3 AND clock_out_at IS NULL
         RETURNING id`,
        [req.body.location || null, tenantId, userId]
      );
      if (updateRes.rowCount === 0) {
        await client.query('ROLLBACK');
        return res.status(409).json({ error: 'no_open_punch_to_clock_out' });
      }
      punchId = updateRes.rows[0].id;
    }

    // Record idempotency
    await client.query(
      `INSERT INTO webhook_idempotency (idempotency_key, tenant_id, user_id, punch_id)
       VALUES ($1, $2, $3, $4)`,
      [idempotencyKey, tenantId, userId, punchId]
    );

    await client.query('COMMIT');
    return res.status(200).json({ ok: true, punch_id: punchId });
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Unique violation on idx_one_open_punch_per_user or webhook_idempotency primary key
    if (err.code === '23505') {
      return res.status(200).json({ ok: true, replayed: true });
    }
    console.error('Webhook processing failed', { err, idempotencyKey });
    return res.status(500).json({ error: 'processing_failed' });
  } finally {
    client.release();
  }
});

export default router;
```

Notes:
- Use `X-Idempotency-Key` header when possible; fallback to LINE message id.
- The partial unique index enforces “one open punch per user” at DB level (strong guarantee).
- Idempotency table + transactional upsert makes retries safe and replayable.
- Return 200 on duplicates so LINE stops retrying.

## 4. Verification

1. Apply schema migration:
   ```bash
   psql workio < server/src/db/schema.sql
   ```
2. Start backend:
   ```bash
   cd server && npm run dev
   ```
3. Simulate duplicate delivery with curl:
   ```bash
   KEY=msg_12345
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Idempotency-Key: $KEY" \
     -d '{"events":[{"source":{"userId":"U123"},"message":{"text":"in"},"timestamp":1712345678901}]}'
   ```
   - First call → 200, punch created.
   - Second call with same KEY → 200, `replayed: true`, same punch_id.
4. Verify DB constraints:
   ```sql
   SELECT * FROM webhook_idempotency WHERE idempotency_key = 'msg_12345';
   SELECT * FROM punches WHERE user_id = <id> ORDER BY created_at DESC LIMIT 2;
   ```
   - Exactly one open punch per user (clock_out_at NULL) at any time.
5. Simulate race: run two concurrent requests with same KEY; confirm only one punch inserted and no 500 errors.
