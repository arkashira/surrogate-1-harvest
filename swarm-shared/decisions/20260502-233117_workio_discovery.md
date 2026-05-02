# workio / discovery

## Final consolidated solution (best of both proposals)

### 1. Diagnosis (merged)
- No idempotency key on punch records → LINE webhook redeliveries and concurrent deliveries create duplicate clock‑in/out rows.
- Non‑atomic read‑then‑insert for punch state → races produce multiple active punches for the same `user_id`/`date`.
- Missing unique constraints to enforce one active punch per user and to deduplicate by delivery → allows invalid double‑punch states at DB level.
- No request deduplication layer (in‑memory or Redis) for fast‑path suppression within short windows.
- No audit trail for delivery attempts → hard to debug duplicates or reconcile state after outages.
- Handler does not return early on duplicates → wastes compute and can send duplicate LINE replies.

### 2. Scope
- **File**: `workio/server/src/routes/line/webhook.ts` (webhook handler)
- **Scope**: add idempotency handling using `line_delivery_id`, atomic upsert with unique constraint, partial unique index for active punches, optional fast‑path dedupe, and audit columns. Return early on duplicates.

### 3. Implementation

#### Step 1 — DB schema (run once)
```sql
-- Idempotency column (nullable; only set when we have a delivery ID)
ALTER TABLE punches ADD COLUMN IF NOT EXISTS line_delivery_id VARCHAR(255);
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_delivery_id
  ON punches (line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;

-- At most one active punch per user/tenant (clock_out_ts IS NULL)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_active
  ON punches (user_id, tenant_id)
  WHERE clock_out_ts IS NULL;

-- Optional audit columns for traceability
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS last_line_event_id VARCHAR(255),
  ADD COLUMN IF NOT EXISTS last_line_timestamp TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS line_webhook_received_at TIMESTAMPTZ DEFAULT NOW();
```

#### Step 2 — Webhook handler (Node + Express + pg)
```ts
// workio/server/src/routes/line/webhook.ts
import { Request, Response } from 'express';
import { pool } from '../../db';
import { v4 as uuidv4 } from 'uuid';

const RECENT_TTL_MS = 10_000;
const recentDeliveryIds = new Set<string>();

function trackRecent(id: string) {
  if (recentDeliveryIds.has(id)) return true;
  recentDeliveryIds.add(id);
  setTimeout(() => recentDeliveryIds.delete(id), RECENT_TTL_MS);
  return false;
}

export async function handleLineWebhook(req: Request, res: Response) {
  const body = req.body;
  const events = body.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(200).json({ status: 'ok' });
  }

  // Validate LINE signature here (existing logic)
  // if (!validateSignature(req)) return res.status(401).end();

  for (const ev of events) {
    if (ev.type !== 'message' || ev.message?.type !== 'text') continue;

    const text = ev.message.text || '';
    const isClockIntent = /(clock|in|out)/i.test(text);
    if (!isClockIntent) continue;

    const userId = ev.source?.userId;
    const timestamp = ev.timestamp;
    if (!userId || !timestamp) continue;

    const tenantId = 'default'; // resolve from user mapping
    const deliveryId = `${userId}-${timestamp}`;
    const now = new Date();
    const isClockOut = /(out)/i.test(text);

    // Fast-path dedupe (same process)
    if (trackRecent(deliveryId)) {
      continue;
    }

    try {
      await pool.query('BEGIN');

      // Idempotent insert: if delivery exists, skip creation
      const insertResult = await pool.query(
        `INSERT INTO punches (user_id, tenant_id, clock_in_ts, line_delivery_id, last_line_event_id, last_line_timestamp, line_webhook_received_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7)
         ON CONFLICT (line_delivery_id) DO NOTHING
         RETURNING id, clock_out_ts`,
        [userId, tenantId, now, deliveryId, ev.eventId || deliveryId, new Date(timestamp), now]
      );

      const isDuplicateDelivery = insertResult.rowCount === 0;
      let punchId: string | null = null;
      let wasAlreadyClosed = false;

      if (!isDuplicateDelivery) {
        punchId = insertResult.rows[0].id;
        wasAlreadyClosed = !!insertResult.rows[0].clock_out_ts;
      } else {
        // Fetch existing punch for this delivery to allow safe clock-out handling
        const fetchResult = await pool.query(
          `SELECT id, clock_out_ts FROM punches WHERE line_delivery_id = $1`,
          [deliveryId]
        );
        if (fetchResult.rowCount > 0) {
          punchId = fetchResult.rows[0].id;
          wasAlreadyClosed = !!fetchResult.rows[0].clock_out_ts;
        }
      }

      // Clock-out handling (only if there's an active punch for this user)
      if (isClockOut) {
        const updateResult = await pool.query(
          `UPDATE punches
           SET clock_out_ts = $1, last_line_event_id = $2, last_line_timestamp = $3
           WHERE user_id = $4 AND tenant_id = $5 AND clock_out_ts IS NULL
           RETURNING id`,
          [now, ev.eventId || deliveryId, new Date(timestamp), userId, tenantId]
        );

        // If no active punch exists and this delivery created none, optionally log or create corrective record
        // For safety, do not auto-create a new punch on clock-out-only messages without a prior active punch.
      }

      await pool.query('COMMIT');

      // Optional: send LINE reply (skip for duplicates to avoid double messages)
      // if (!isDuplicateDelivery && ev.replyToken) {
      //   await replyLine(ev.replyToken, isClockOut ? 'Clocked out' : 'Clocked in');
      // }
    } catch (err) {
      await pool.query('ROLLBACK');
      console.error('Punch handling failed', err);
      // Return 500 so LINE may retry (idempotency protects duplicates)
      return res.status(500).json({ error: 'internal' });
    }
  }

  return res.status(200).json({ status: 'ok' });
}
```

#### Step 3 — Optional Redis fast-path (production)
Replace in-memory `recentDeliveryIds` with a Redis SET/GETEX with short TTL for multi-instance safety:
```ts
// pseudo
const key = `line_delivery:${deliveryId}`;
if (await redis.set(key, '1', 'PX', RECENT_TTL_MS, 'NX')) {
  // not recent -> process
} else {
  // duplicate -> skip
}
```

### 4. Verification

1. **Schema check**
   ```bash
   psql workio -c "\d punches"
   psql workio -c "\di idx_punches*"
   ```

2. **Duplicate delivery test**
   ```bash
   payload='{"events":[{"type":"message","message":{"type":"text","text":"clock in"},"source":{"userId":"U123"},"timestamp":1712345678901,"eventId":"E123"}]}'
   curl -X POST http://localhost:3000/webhook/line -H "Content-Type: application/json" -d "$payload"
   curl -X POST http://localhost:3000/webhook/line -H "Content-Type: application/json" -d "$payload"
   psql workio -c "SELECT count(*) FROM punches WHERE user_id='U123';"
   # expect 1
   ```

3. **Race test**
   ```bash
   hey -n 20 -c 5 -m POST -H "Content-Type: application/json" -d "$payload" http://localhost:3000/webhook/line
   psql workio -c "SELECT * FROM punches WHERE user_id='U123' ORDER BY clock_in_ts;"
   # expect exactly one active punch (clock_out_ts IS NULL) and no constraint violations in
