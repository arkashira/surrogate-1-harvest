# workio / discovery

## 1. Diagnosis

- Missing **LINE webhook signature verification** (`X-Line-Signature`) on `/webhook/line` allows spoofed/replayed events to be accepted as valid clock-in/out or leave requests.
- No **idempotency/replay protection** — LINE retries on 5xx/timeouts can create duplicate `attendance_punches`, `leave_requests`, `ot_requests`.
- No **request deduplication key** in DB layer (no unique constraint/index on `line_event_id` or similar) to prevent duplicates at storage level.
- Webhook handler does not validate **event timestamp skew** (replay window) — old events could be replayed after legitimate retries.
- Missing **early rejection path** for invalid signatures (returns 200 on bad sig instead of 401), which encourages retry storms.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (or equivalent route file)  
Scope:
- Add `crypto`-based HMAC-SHA256 signature verification using `channelSecret`.
- Add idempotency check using `event.source.userId + event.timestamp + event.type` or `event.message?.id`/`event.postback?.data` hash as dedupe key.
- Add DB unique constraint/index on dedupe key column (or use existing `line_event_id` if present).
- Return `401` on invalid signature, `409` on duplicate, `200` only after successful processing.

## 3. Implementation

```ts
// /opt/axentx/workio/server/src/routes/webhook/line.ts
import crypto from 'crypto';
import express from 'express';
import { pool } from '../db';

const router = express.Router();
const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

function verifySignature(body: string, signature: string): boolean {
  if (!CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(body)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function buildDedupeKey(event: any): string {
  // Prefer message id when present; fallback to postback data + timestamp; last resort composite
  if (event.message?.id) return `msg:${event.message.id}`;
  if (event.postback?.data) return `pb:${event.postback.data}:${event.timestamp}`;
  return `evt:${event.source.userId}:${event.type}:${event.timestamp}`;
}

router.post('/line', async (req, res) => {
  const signature = req.get('X-Line-Signature') || '';
  const rawBody = JSON.stringify(req.body);

  // 1) Signature verification
  if (!verifySignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const events = req.body.events || [];
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(200).json({ ok: true });
  }

  // 2) Idempotency check + processing
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const event of events) {
      const dedupeKey = buildDedupeKey(event);

      // Check duplicate
      const dup = await client.query(
        `SELECT 1 FROM webhook_events WHERE dedupe_key = $1`,
        [dedupeKey]
      );
      if (dup.rows.length > 0) {
        // duplicate — skip but continue processing other events
        continue;
      }

      // Record event (store minimal payload for audit)
      await client.query(
        `INSERT INTO webhook_events (dedupe_key, event_type, user_id, payload, created_at)
         VALUES ($1, $2, $3, $4, NOW())`,
        [dedupeKey, event.type, event.source?.userId, event]
      );

      // Route to domain handlers (clock/leave/ot)
      await handleLineEvent(event, client);
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    res.status(500).json({ error: 'Processing failed' });
  } finally {
    client.release();
  }
});

async function handleLineEvent(event: any, client: any) {
  // Existing domain logic (clock-in/out, leave, ot) goes here.
  // Ensure downstream handlers are idempotent where possible.
  // Example stub:
  if (event.type === 'message' && event.message?.text === 'clockin') {
    await client.query(
      `INSERT INTO attendance_punches (user_id, punch_type, punched_at, line_event_id)
       VALUES ($1, 'in', NOW(), $2)
       ON CONFLICT DO NOTHING`,
      [event.source.userId, event.message.id]
    );
  }
}

export default router;
```

DB migration (run once):

```sql
-- /opt/axentx/workio/server/src/db/migrations/001_webhook_dedupe.sql
CREATE TABLE IF NOT EXISTS webhook_events (
  id BIGSERIAL PRIMARY KEY,
  dedupe_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  user_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

-- Ensure attendance_punches has unique constraint on line_event_id if used
ALTER TABLE attendance_punches
  ADD COLUMN IF NOT EXISTS line_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_punches_line_event_id
  ON attendance_punches (line_event_id)
  WHERE line_event_id IS NOT NULL;
```

Add to `.env.example`:

```
LINE_CHANNEL_SECRET=
```

## 4. Verification

1. **Signature rejection**  
   Send a POST to `/webhook/line` with any body and a bogus `X-Line-Signature`. Expect `401`.

2. **Duplicate suppression**  
   - Send a valid signed payload (use a small script to compute correct HMAC).  
   - Confirm first request returns `200` and creates `webhook_events` row and expected domain row (e.g., `attendance_punches`).  
   - Replay same payload (same signature). Expect `200` (or `409` if you prefer) and **no new domain row**; `webhook_events` shows one row (unique constraint prevents insert).

3. **Retry safety**  
   Simulate LINE retry by sending same event twice with short delay. Verify only one punch/leave/ot row created.

4. **DB constraints**  
   Attempt to insert duplicate `line_event_id` into `attendance_punches` manually — should be rejected by unique index.

5. **Log audit**  
   Check `webhook_events` table contains dedupe keys and payloads for traceability.
