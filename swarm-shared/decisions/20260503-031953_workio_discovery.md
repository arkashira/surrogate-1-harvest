# workio / discovery

## 1. Diagnosis

- Missing **LINE webhook signature verification** (`X-Line-Signature`) on `/webhook/line` allows spoofed/replayed events to be accepted as valid clock-in/out or leave requests.
- No **idempotency/replay protection** — LINE retries on 5xx/timeouts can create duplicate `attendance_punches`, `leave_requests`, `ot_requests`.
- No **request deduplication key** in DB layer (no unique constraint/index on `line_event_id` or equivalent) to prevent duplicates at storage level.
- Webhook handler accepts events without validating **timestamp tolerance** (replay window), enabling delayed/replayed payloads to be processed.
- Missing **atomic upsert pattern** for punch/leave/OT creation; concurrent retries can race and insert multiple rows for the same logical event.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (or equivalent route file handling `/webhook/line`)

Scope:
- Add `X-Line-Signature` HMAC-SHA256 verification using channel secret from `.env`.
- Add idempotency using `lineEventId` (from `body.events[].source.userId` + `body.events[].timestamp` + `body.events[].type` + `body.events[].replyToken` or the event-level `webhookEventId` if available) stored in a new `webhook_events` table with unique constraint.
- Wrap punch/leave/OT creation in an atomic upsert that checks the dedupe key before side effects.
- Add timestamp tolerance check (e.g., reject events older than 5 minutes).

## 3. Implementation

### 3.1 DB: add webhook_events table and constraints

```sql
-- server/src/db/schema.sql  (append)
CREATE TABLE IF NOT EXISTS webhook_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  line_event_id   TEXT NOT NULL,
  tenant_id       UUID NOT NULL REFERENCES tenants(id),
  event_type      TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  processed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_line_event_id UNIQUE (line_event_id)
);

-- Index for fast lookup during retries
CREATE INDEX IF NOT EXISTS idx_webhook_events_line_event_id ON webhook_events(line_event_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_processed ON webhook_events(processed_at) WHERE processed_at IS NOT NULL;
```

### 3.2 Route: add verification + idempotency middleware

```ts
// server/src/routes/webhook/line.ts
import crypto from 'crypto';
import express, { Request, Response, NextFunction } from 'express';
import { pool } from '../../db';
import { verifyLineSignature, isReplayEvent, markEventProcessed } from '../../lib/line-webhook';

const router = express.Router();

// Middleware: verify signature and reject replays
router.use(async (req: Request, res: Response, next: NextFunction) => {
  const signature = req.get('X-Line-Signature');
  const channelSecret = process.env.LINE_CHANNEL_SECRET;

  if (!signature || !channelSecret) {
    return res.status(401).json({ error: 'Missing signature or secret' });
  }

  if (!verifyLineSignature(channelSecret, JSON.stringify(req.body), signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  // Basic timestamp tolerance (5 min)
  const now = Date.now();
  const body = req.body as { events?: Array<{ timestamp?: number }> };
  if (body.events?.some((e) => e.timestamp && Math.abs(now - e.timestamp) > 5 * 60 * 1000)) {
    return res.status(400).json({ error: 'Event timestamp out of tolerance' });
  }

  // Idempotency check: if any event already processed, skip processing but return 200 (acknowledge)
  try {
    const isReplay = await isReplayEvent(req.body);
    if (isReplay) {
      return res.status(200).json({ ok: true, reason: 'already_processed' });
    }
  } catch (err) {
    // Fail open to avoid dropping legitimate events; log and continue
    console.warn('Idempotency check failed', err);
  }

  next();
});

router.post('/', async (req: Request, res: Response) => {
  const body = req.body;
  const events = body.events || [];

  try {
    for (const ev of events) {
      await handleLineEvent(ev);
    }

    // Mark events processed (best-effort; failure shouldn't roll back processing)
    try {
      await markEventProcessed(body);
    } catch (err) {
      console.warn('Failed to mark events processed', err);
    }

    res.status(200).json({ ok: true });
  } catch (err) {
    console.error('Webhook processing failed', err);
    // Return 500 so LINE will retry (but idempotency will prevent duplicates)
    res.status(500).json({ error: 'Processing failed' });
  }
});

async function handleLineEvent(event: any) {
  // Map event types to domain actions (clock/leave/ot)
  // Example: if event.type === 'message' and event.message.type === 'text'
  //   parse command and create punch/leave/ot with dedupe guard

  // Dedupe guard: use atomic upsert on attendance_punches/leave_requests/ot_requests
  // Example for attendance_punches:
  //   INSERT INTO attendance_punches (line_event_id, user_id, tenant_id, type, ts, location, created_at)
  //   VALUES ($1,$2,$3,$4,$5,$6,NOW())
  //   ON CONFLICT (line_event_id) DO NOTHING;
}

export default router;
```

### 3.3 Lib: signature + idempotency helpers

```ts
// server/src/lib/line-webhook.ts
import crypto from 'crypto';
import { pool } from '../db';

export function verifyLineSignature(channelSecret: string, body: string, signature: string): boolean {
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(body, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

export async function isReplayEvent(body: any): Promise<boolean> {
  const events = body.events || [];
  if (events.length === 0) return false;

  // Build stable line_event_id candidates from event fields
  // Prefer webhookEventId if provided; otherwise composite key
  const ids = events.map((e: any) => e.webhookEventId || [e.type, e.source?.userId, e.timestamp, e.replyToken].filter(Boolean).join(':'));

  const client = await pool.connect();
  try {
    const placeholders = ids.map((_: any, i: number) => `$${i + 1}`).join(',');
    const result = await client.query(
      `SELECT line_event_id FROM webhook_events WHERE line_event_id IN (${placeholders})`,
      ids
    );
    return result.rows.length > 0;
  } finally {
    client.release();
  }
}

export async function markEventProcessed(body: any): Promise<void> {
  const events = body.events || [];
  if (events.length === 0) return;

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    for (const ev of events) {
      const lineEventId = ev.webhookEventId || [ev.type, ev.source?.userId, ev.timestamp, ev.replyToken].filter(Boolean).join(':');
      const payloadHash = crypto.createHash('sha256').update(JSON.stringify(ev)).digest('hex');
      const tenantId = await inferTenantIdFromEvent(ev); // implement per your tenant mapping

      await client.query(
        `INSERT INTO webhook_events (line_event_id, tenant_id, event_type, payload_hash)
         VALUES ($1,$2,$3,$4)
         ON CONFLICT (line_event_id) DO UPDATE
         SET payload_hash = EXCLUDED.payload_hash, processed_at = NOW()`,
        [lineEventId, tenantId, ev.type, payloadHash]
      );
    }
    await client.query('COMMIT');
  } catch (err) {
