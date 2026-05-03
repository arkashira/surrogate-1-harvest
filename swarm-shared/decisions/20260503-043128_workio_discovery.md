# workio / discovery

## 1. Diagnosis
- **Missing `X-Line-Signature` verification** on webhook endpoint — accepts spoofed events enabling attendance fraud and phantom clock-ins/outs.
- **No idempotency guard** against LINE webhook replays (5xx/timeouts) — duplicate clock-in/out or leave/OT records created on retry.
- **No request deduplication key** stored with events — retry storms create multiple rows for same user/timestamp.
- **Webhook handler returns 200 before DB commit** — LINE treats as success even if persistence fails, causing silent data loss.
- **No defense against replay window abuse** — same event can be replayed hours/days later if signature not time-bound.

## 2. Proposed change
File: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (or create if missing)  
Scope: add `X-Line-Signature` crypto verification + idempotency table + atomic commit-before-ack pattern.

## 3. Implementation

```bash
# Ensure file exists and is ready
mkdir -p /opt/axentx/workio/server/src/routes/webhook
touch /opt/axentx/workio/server/src/routes/webhook/line.ts
```

```ts
// /opt/axentx/workio/server/src/routes/webhook/line.ts
import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import { pool } from '../../db';

const router = Router();
const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';
const IDEMPOTENCY_WINDOW_MS = 5 * 60 * 1000; // 5m; align with LINE retry window

function verifySignature(body: string, signature: string): boolean {
  if (!CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(body, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

async function isDuplicate(eventId: string): Promise<boolean> {
  const { rows } = await pool.query(
    `SELECT 1 FROM line_webhook_events
     WHERE event_id = $1
       AND created_at > NOW() - INTERVAL '${IDEMPOTENCY_WINDOW_MS} milliseconds'`,
    [eventId]
  );
  return rows.length > 0;
}

async function recordEvent(eventId: string, payload: unknown): Promise<void> {
  await pool.query(
    `INSERT INTO line_webhook_events (event_id, payload, created_at)
     VALUES ($1, $2, NOW())`,
    [eventId, payload]
  );
}

router.post('/line', async (req: Request, res: Response) => {
  const signature = req.headers['x-line-signature'] as string || '';
  const rawBody = JSON.stringify(req.body);

  // 1) Signature verification
  if (!verifySignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const events = req.body?.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(400).json({ error: 'No events' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      const eventId = ev?.source?.userId && ev?.timestamp
        ? `${ev.source.userId}:${ev.type}:${ev.timestamp}:${ev?.replyToken || ''}`
        : ev?.webhookEventId || crypto.randomUUID();

      // 2) Idempotency check
      if (await isDuplicate(eventId)) {
        continue; // skip duplicate but still ack batch
      }

      // 3) Persist event atomically with domain action
      await recordEvent(eventId, ev);

      // 4) Domain handling (clock-in/out, leave/OT) — minimal safe stub
      // Replace with real handlers; keep atomic inside this transaction
      if (ev.type === 'message' && ev.message?.type === 'text') {
        // Example: parse clock-in command
        const text = ev.message.text.trim().toLowerCase();
        if (text === 'clock in' || text === 'clock out') {
          await client.query(
            `INSERT INTO attendances (user_id, event_type, line_event_id, created_at)
             VALUES ($1, $2, $3, NOW())
             ON CONFLICT DO NOTHING`,
            [ev.source.userId, text === 'clock in' ? 'in' : 'out', eventId]
          );
        }
      }
    }

    await client.query('COMMIT');
    // 5) Only after commit do we acknowledge LINE
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    // 6) Non-2xx causes LINE retry (per docs)
    res.status(500).json({ error: 'Processing failed' });
  } finally {
    client.release();
  }
});

export default router;
```

DB migration (run once):

```sql
-- /opt/axentx/workio/server/src/db/migrations/001_line_webhook_idempotency.sql
CREATE TABLE IF NOT EXISTS line_webhook_events (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_line_event_id
ON line_webhook_events (event_id);

-- Optional: index for fast windowed dedupe
CREATE INDEX IF NOT EXISTS idx_line_event_created
ON line_webhook_events (event_id, created_at);
```

Wire into Express app (if not already):

```ts
// /opt/axentx/workio/server/src/app.ts (or index.ts)
import lineWebhook from './routes/webhook/line';
app.use('/webhook', lineWebhook);
```

## 4. Verification

1. **Signature rejection test**  
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: invalidsig" \
     -d '{"events":[{"type":"message","source":{"userId":"U123"},"timestamp":1712345678900,"message":{"type":"text","text":"clock in"}}]}'
   ```
   Expect `401` with `Invalid signature`.

2. **Valid signature acceptance (local dev)**  
   Compute valid sig with channel secret and resend same payload with correct `X-Line-Signature`. Expect `200` and row in `line_webhook_events`.

3. **Idempotency test**  
   Replay same signed payload within 5 minutes. Expect second request to return `200` but no new attendance row (check `attendances` count and `line_webhook_events` — event_id should be unique).

4. **Rollback on DB error**  
   Temporarily break DB constraint or kill connection mid-handler; ensure route returns `500` and no partial rows committed.

5. **LINE retry simulation**  
   Return 500 intentionally for one event; confirm LINE retry behavior (logs/time) and that dedupe prevents duplicates once fixed.
