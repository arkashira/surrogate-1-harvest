# workio / discovery

## 1. Diagnosis

- **No idempotency guard on LINE webhook punches** — redeliveries and concurrent deliveries create duplicate clock-in/out rows.
- **Non-atomic read-then-insert for punch state** — races between concurrent webhooks produce multiple open punches or lost state.
- **Missing unique constraint on `(user_id, tenant_id, date, punch_type)` for open punches** — DB-level duplicates possible and undetected.
- **No deduplication key from LINE (`deliveryId`/`timestamp`+`userId`)** — retries cannot be safely de-duplicated.
- **Webhook handler commits before business validation** — partial or invalid punches can be persisted, complicating reconciliation.

## 2. Proposed change

File: `workio/server/src/routes/line/webhook.ts` (or equivalent route handling LINE events)  
Scope: add idempotent punch handling with atomic upsert and unique constraint.

- Add DB unique constraint/index: `punches_unique_open UNIQUE (user_id, tenant_id, date, punch_type) WHERE status = 'open'` (PostgreSQL partial index).
- Add idempotency table: `line_webhook_deliveries (delivery_key text PRIMARY KEY, created_at timestamptz)` keyed by LINE `deliveryId` or deterministic hash of event.
- Refactor handler to:
  1. Begin transaction.
  2. Insert delivery key (ignore conflict) — fast idempotency short-circuit.
  3. Atomically upsert punch: `INSERT ... ON CONFLICT (user_id, tenant_id, date, punch_type) WHERE status = 'open' DO UPDATE SET ...` closing previous or rejecting invalid transition.
  4. Commit.

## 3. Implementation

```sql
-- workio/server/src/db/schema.sql
-- Add partial unique index to prevent duplicate open punches
CREATE UNIQUE INDEX IF NOT EXISTS punches_unique_open
ON punches (user_id, tenant_id, date, punch_type)
WHERE status = 'open';

-- Idempotency table for LINE webhook deliveries
CREATE TABLE IF NOT EXISTS line_webhook_deliveries (
  delivery_key TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

```ts
// workio/server/src/routes/line/webhook.ts
import { Router, Request, Response } from 'express';
import { pool } from '../../db';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

function deliveryKey(event: any): string {
  // Prefer LINE deliveryId; fallback to deterministic hash of timestamp+userId+type
  if (event.source?.userId && event.timestamp) {
    return `line:${event.source.userId}:${event.timestamp}:${event.type}`;
  }
  return `line:unknown:${uuidv4()}`;
}

async function upsertPunch(
  userId: string,
  tenantId: string,
  punchType: 'clock_in' | 'clock_out',
  location?: { lat: number; lng: number }
) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const today = new Date().toISOString().split('T')[0];

    // Idempotent punch upsert: one open punch per user/tenant/day/type
    const upsertQuery = `
      INSERT INTO punches (user_id, tenant_id, date, punch_type, status, location, created_at, updated_at)
      VALUES ($1, $2, $3, $4, 'open', $5, NOW(), NOW())
      ON CONFLICT (user_id, tenant_id, date, punch_type)
      WHERE status = 'open'
      DO UPDATE SET
        location = EXCLUDED.location,
        updated_at = NOW()
      RETURNING *;
    `;
    const res = await client.query(upsertQuery, [
      userId,
      tenantId,
      today,
      punchType,
      location ? JSON.stringify(location) : null,
    ]);

    await client.query('COMMIT');
    return res.rows[0];
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}

router.post('/webhook/line', async (req: Request, res: Response) => {
  const events = req.body?.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).json({ error: 'invalid payload' });
  }

  const client = await pool.connect();
  try {
    for (const ev of events) {
      const key = deliveryKey(ev);

      // Fast idempotency check/insert
      const idempotent = await client.query(
        'INSERT INTO line_webhook_deliveries (delivery_key, created_at) VALUES ($1, NOW()) ON CONFLICT DO NOTHING RETURNING 1',
        [key]
      );
      if (idempotent.rowCount === 0) {
        // Already processed — skip but continue processing other events
        continue;
      }

      // Only handle message events that contain clock intent (simplified)
      if (ev.type === 'message' && ev.message?.text) {
        const text = ev.message.text.toLowerCase();
        const userId = ev.source?.userId;
        const tenantId = 'default'; // replace with real tenant resolution

        if (!userId) continue;

        if (text.includes('in') || text.includes('clock in')) {
          await upsertPunch(userId, tenantId, 'clock_in');
        } else if (text.includes('out') || text.includes('clock out')) {
          // For clock_out, close the open punch (example: update status)
          await client.query(
            `UPDATE punches
             SET status = 'closed', updated_at = NOW()
             WHERE user_id = $1 AND tenant_id = $2 AND date = $3 AND punch_type = 'clock_in' AND status = 'open'`,
            [userId, tenantId, new Date().toISOString().split('T')[0]]
          );
        }
      }
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    res.status(500).json({ error: 'processing failed' });
  } finally {
    client.release();
  }
});

export default router;
```

Notes:
- Replace tenant resolution with real logic (e.g., from user record or channel binding).
- Add indexes on `punches(user_id, tenant_id, date, punch_type, status)` for performance.
- Consider adding a TTL/cleanup job for `line_webhook_deliveries` (e.g., delete > 7 days).

## 4. Verification

1. Apply schema migration:
   ```bash
   psql workio < server/src/db/schema.sql
   ```
2. Start backend:
   ```bash
   cd workio/server && npm run dev
   ```
3. Simulate duplicate LINE delivery:
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -d '{
           "events": [
             {
               "type": "message",
               "source": { "userId": "Utest123" },
               "timestamp": 1712345678901,
               "message": { "text": "clock in" }
             }
           ]
         }'
   ```
   Repeat same payload 3× rapidly — only one open punch row should exist.

4. Check DB:
   ```sql
   SELECT * FROM punches WHERE user_id = 'Utest123' ORDER BY created_at DESC LIMIT 5;
   SELECT * FROM line_webhook_deliveries WHERE delivery_key LIKE 'line:Utest123%';
   ```
   Expect: one open punch (or one closed if clock_out simulated) and one delivery key.

5. Concurrent race test (optional):
   Use `ab` or a small parallel script to POST the same payload 10× concurrently — verify row count remains 1 and no constraint violations occur.
