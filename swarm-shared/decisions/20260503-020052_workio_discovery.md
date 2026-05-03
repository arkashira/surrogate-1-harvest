# workio / discovery

## 1. Diagnosis

- **No idempotency key on LINE webhook handler** — identical `X-Line-Signature` causes retries to create duplicate clock-in/out records.
- **Race-prone upsert** — `findOne → update/insert` is non-atomic; concurrent replays or double-taps corrupt punch state (e.g., two “in” records without intervening “out”).
- **Missing transactional boundary** — punch write and state transition (last punch) are not atomic; partial failures leave inconsistent employee state.
- **No deduplication window** — retries within seconds/minutes are treated as new punches instead of being suppressed.
- **Schema lacks uniqueness guard** — no unique constraint/index to prevent duplicate `(employee_id, timestamp, event_type)` for the same logical punch.

## 2. Proposed change

- **File:** `workio/server/src/routes/webhook/line.ts` (or equivalent webhook handler)
- **Scope:** Add idempotency handling for LINE webhook events using `X-Line-Signature` + short-term deduplication and atomic upsert for punches.

## 3. Implementation

```bash
# Ensure migration tooling exists (if not, use raw SQL)
cd /opt/axentx/workio/server
```

**Migration: add idempotency table + unique constraint on punches**

```sql
-- server/src/db/migrations/20260504_idempotency.sql
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key VARCHAR(255) PRIMARY KEY,
  event_type      VARCHAR(64)  NOT NULL,
  payload_hash    VARCHAR(64)  NOT NULL,
  processed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  response_status SMALLINT     NOT NULL,
  response_body   TEXT
);

-- Optional: keep punch uniqueness per employee+time+type (coalesce duplicates)
-- If table name is punches and has employee_id, timestamp, event_type
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_punch
ON punches (employee_id, timestamp, event_type);
```

Apply:
```bash
psql workio < server/src/db/migrations/20260504_idempotency.sql
```

**Handler diff (conceptual):** `server/src/routes/webhook/line.ts`

```ts
import { Request, Response } from 'express';
import crypto from 'crypto';
import { pool } from '../../db';

const IDEMPOTENCY_TTL_MS = 5 * 60 * 1000; // 5m dedup window

function hashPayload(body: any): string {
  return crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
}

export async function lineWebhook(req: Request, res: Response) {
  const sig = req.get('X-Line-Signature');
  const body = req.body;

  if (!sig || !body.events?.length) {
    return res.status(400).send('Bad request');
  }

  const payloadHash = hashPayload(body);

  // Use a client for transaction
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check
    const idempotencyKey = `line:${sig}`;
    const exists = await client.query(
      `SELECT 1 FROM line_webhook_idempotency WHERE idempotency_key = $1 AND processed_at > NOW() - ($2::text || ' ms')::interval`,
      [idempotencyKey, IDEMPOTENCY_TTL_MS]
    );

    if (exists.rows.length > 0) {
      await client.query('ROLLBACK');
      return res.status(200).json({ ok: true, reason: 'duplicate-suppressed' });
    }

    // Process each event (simplify to clock in/out)
    for (const ev of body.events) {
      if (ev.type !== 'message' || !ev.message?.text) continue;

      const text = ev.message.text.trim().toLowerCase();
      const userId = ev.source.userId;
      const ts = new Date(ev.timestamp);

      // Map to employee (assumes mapping exists)
      const emp = await client.query(
        'SELECT id FROM employees WHERE line_user_id = $1 LIMIT 1',
        [userId]
      );

      if (!emp.rows.length) continue;
      const employeeId = emp.rows[0].id;

      // Determine event type
      const eventType = text.includes('in') || text.includes('clock in') ? 'clock_in' :
                        text.includes('out') || text.includes('clock out') ? 'clock_out' : null;

      if (!eventType) continue;

      // Atomic upsert: insert punch, ignore duplicates via unique constraint
      await client.query(
        `INSERT INTO punches (employee_id, timestamp, event_type, line_event_id, created_at)
         VALUES ($1, $2, $3, $4, NOW())
         ON CONFLICT (employee_id, timestamp, event_type) DO NOTHING`,
        [employeeId, ts, eventType, ev.message.id]
      );
    }

    // Record idempotency
    await client.query(
      `INSERT INTO line_webhook_idempotency (idempotency_key, event_type, payload_hash, processed_at, response_status, response_body)
       VALUES ($1, $2, $3, NOW(), $4, $5)`,
      [idempotencyKey, 'line_webhook', payloadHash, 200, 'OK']
    );

    await client.query('COMMIT');
    return res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook error', err);
    return res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
}
```

**Register route** (if not already):
```ts
// server/src/routes/webhook/index.ts
import { Router } from 'express';
import { lineWebhook } from './line';

const router = Router();
router.post('/line', lineWebhook);
export default router;
```

**Environment:** ensure `pool` is configured and `employees.line_user_id` + `punches` table exist.

## 4. Verification

1. **Apply migration**
   ```bash
   psql workio < server/src/db/migrations/20260504_idempotency.sql
   ```

2. **Start backend**
   ```bash
   cd /opt/axentx/workio/server
   npm run dev
   ```

3. **Simulate duplicate webhook**
   ```bash
   WEBHOOK_URL="http://localhost:3000/webhook/line"
   BODY='{"events":[{"type":"message","message":{"id":"123","text":"clock in"},"source":{"userId":"U123"},"timestamp":1714828800000}]}'
   SIG="testsig123"

   # First request
   curl -X POST "$WEBHOOK_URL" \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: $SIG" \
     -d "$BODY"

   # Immediate duplicate (same sig+body)
   curl -X POST "$WEBHOOK_URL" \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: $SIG" \
     -d "$BODY"
   ```

   Expected: first returns `{"ok":true}`, second returns `{"ok":true,"reason":"duplicate-suppressed"}` and does not create a second punch.

4. **Check DB**
   ```sql
   SELECT employee_id, event_type, timestamp FROM punches WHERE line_event_id = '123';
   ```
   Should return exactly one row.

5. **Concurrency test (optional)**
   Use `ab` or a small parallel script to fire two requests at the same time; verify only one punch row is created and no constraint violation errors occur.
