# workio / discovery

## 1. Diagnosis
- **No request/response observability** for `/webhook/line`: payloads, signature verification result, and processing outcome are not logged, making retries and spoofing invisible.
- **No idempotency guard** on LINE webhook handler: retries on 5xx/timeouts can replay clock-in/out or leave/OT events and corrupt state (duplicate punches, double approvals).
- **No client-side state sync**: frontend relies on local React state + manual refresh; multi-tab/devices diverge when LINE webhooks commit state in the backend.
- **No structured activity stream**: downstream features (audit, dashboard, real-time feed) lack an immutable event log with tenant context.
- **Missing latency/error telemetry**: operators cannot detect silent failures, retry storms, or LINE delivery issues.

## 2. Proposed change
Add an **idempotent, observable webhook ingestion layer** in the backend:
- File: `workio/server/src/routes/lineWebhook.ts` (or equivalent)
- Scope:
  - Verify `X-Line-Signature` on every request.
  - Store raw webhook payload + verification result + processing outcome in `webhook_events` table (idempotent key = `X-Line-Retry-Id` or hash of body+event.id).
  - Emit normalized domain events (`clock_in`, `leave_request`, etc.) to an internal event bus/stream.
  - Return 200 only after successful DB commit; 4xx on signature failure; 409 on duplicate idempotency key.
- Secondary: lightweight SSE/WS endpoint so frontend can subscribe to real-time tenant events (optional for this 2h scope).

## 3. Implementation

### 3.1 DB migration (add idempotency + observability)
```sql
-- server/src/db/migrations/001_webhook_events.sql
CREATE TABLE IF NOT EXISTS webhook_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id),
  event_type      TEXT NOT NULL,            -- 'clock_in', 'clock_out', 'leave_request', etc.
  raw_payload     JSONB NOT NULL,
  line_signature  TEXT,
  verified        BOOLEAN NOT NULL DEFAULT FALSE,
  idempotency_key TEXT NOT NULL,            -- X-Line-Retry-Id or sha256(body + event.id)
  processed_at    TIMESTAMPTZ,
  status          TEXT NOT NULL DEFAULT 'received', -- received, verified, processed, duplicate, rejected
  error_detail    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_idempotency UNIQUE (idempotency_key)
);

-- Index for fast duplicate checks and tenant-time queries
CREATE INDEX IF NOT EXISTS idx_webhook_events_tenant_created ON webhook_events(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_webhook_events_idempotency ON webhook_events(idempotency_key);
```

### 3.2 Idempotent, verified webhook handler
```ts
// server/src/routes/lineWebhook.ts
import crypto from 'crypto';
import express from 'express';
import db from '../db';
import { processLineEvent } from '../services/lineEventProcessor';

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

function idempotencyKey(headers: any, body: string): string {
  // Prefer LINE's retry id when present; otherwise deterministic hash
  if (headers['x-line-retry-id']) return String(headers['x-line-retry-id']);
  const parsed = JSON.parse(body);
  const eventIds = (parsed.events || []).map((e: any) => e.id).join(',');
  return crypto.createHash('sha256').update(body + eventIds).digest('hex');
}

router.post('/webhook/line', async (req, res) => {
  const rawBody = JSON.stringify(req.body);
  const signature = req.headers['x-line-signature'] as string;
  const retryKey = idempotencyKey(req.headers, rawBody);
  const verified = verifySignature(rawBody, signature);

  // Try insert idempotency record first (fast duplicate rejection)
  try {
    await db.query(
      `INSERT INTO webhook_events (tenant_id, event_type, raw_payload, line_signature, verified, idempotency_key, status)
       VALUES ($1, $2, $3, $4, $5, $6, $7)`,
      [null, 'line_webhook', rawBody, signature, verified, retryKey, 'received']
    );
  } catch (e: any) {
    if (e.code === '23505') {
      // Duplicate idempotency key: log and acknowledge (idempotent success)
      console.warn(`Duplicate LINE webhook rejected: ${retryKey}`);
      return res.status(200).json({ ok: true, reason: 'duplicate' });
    }
    console.error('Failed to store webhook event', e);
    return res.status(500).json({ error: 'storage_error' });
  }

  // Reject unverified requests early
  if (!verified) {
    await db.query(
      `UPDATE webhook_events SET status='rejected', error_detail='invalid_signature' WHERE idempotency_key=$1`,
      [retryKey]
    );
    return res.status(401).json({ error: 'invalid_signature' });
  }

  // Process domain events
  const parsed = JSON.parse(rawBody);
  const events = parsed.events || [];
  try {
    for (const ev of events) {
      await processLineEvent(ev, retryKey);
    }

    await db.query(
      `UPDATE webhook_events SET status='processed', processed_at=now() WHERE idempotency_key=$1`,
      [retryKey]
    );
    res.status(200).json({ ok: true });
  } catch (err: any) {
    await db.query(
      `UPDATE webhook_events SET status='failed', error_detail=$1 WHERE idempotency_key=$2`,
      [err.message, retryKey]
    );
    // Return 5xx so LINE will retry (per their retry policy)
    console.error('LINE webhook processing failed', err);
    res.status(500).json({ error: 'processing_failed' });
  }
});

export default router;
```

### 3.3 Minimal domain processor (example)
```ts
// server/src/services/lineEventProcessor.ts
import db from '../db';

export async function processLineEvent(event: any, retryKey: string) {
  const type = event.type; // 'message', 'postback', etc.
  const userId = event.source?.userId;
  const timestamp = event.timestamp;

  // Map LINE payloads to domain actions (clock/leave/ot) via postback data or message text
  // Example: postback with data "action=clock_in&tenant=xxx"
  if (type === 'postback') {
    const data = Object.fromEntries(new URLSearchParams(event.postback.data).entries());
    if (data.action === 'clock_in' || data.action === 'clock_out') {
      await db.query(
        `INSERT INTO clock_events (tenant_id, user_id, event_type, line_event_id, occurred_at, idempotency_key)
         VALUES ($1, $2, $3, $4, to_timestamp($5 / 1000.0), $6)
         ON CONFLICT (idempotency_key) DO NOTHING`,
        [data.tenant, userId, data.action, event.id, timestamp, retryKey + ':' + event.id]
      );
    }
  }

  // Extend with leave/OT flows similarly; keep idempotent keys derived from event.id
}
```

### 3.4 Wire into app
```ts
// server/src/app.ts (or index)
import lineWebhookRouter from './routes/lineWebhook';
app.use('/webhook/line', lineWebhookRouter);
```

## 4. Verification
1. **Schema applied**: run migration and confirm `webhook_events` exists and has unique constraint on `idempotency_key`.
2. **Signature verification**:
   - Send a POST with valid signature → 200 and `status='processed'
