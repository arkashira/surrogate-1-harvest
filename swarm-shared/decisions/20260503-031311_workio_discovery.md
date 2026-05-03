# workio / discovery

## Final Synthesized Solution (Correct + Actionable)

### 1. Diagnosis (merged, prioritized)
- **Security**: Missing `X-Line-Signature` verification allows spoofed clock/leave/OT events.
- **Reliability**: No idempotency/replay protection — LINE retries create duplicate `clock_events`, `leaves`, `ot_requests`.
- **Data integrity**: No server-side validation of clock-in/out pairing (double clock-in, missing clock-out).
- **UX**: Frontend lacks optimistic state + local pending queue; double-taps during latency create duplicate intents.
- **Observability**: No structured logging/metrics on webhook ingestion (can’t distinguish replays, retries, or attacks).
- **Onboarding**: `.env.example` and docs omit required secrets/behavior (`LINE_CHANNEL_SECRET`, idempotency TTL).

---

### 2. Proposed Change (single coherent plan)
- **Backend** (`/opt/axentx/workio/server`):
  - Add **LINE signature verification middleware** (HMAC-SHA256, constant-time compare).
  - Add **idempotency layer** keyed by `line_event_id` with unique constraint + short TTL (7d) and immediate 200 for duplicates to stop LINE retries.
  - Add **server-side validation** for clock-in/out pairing before persisting.
  - Add **structured logging + metrics** for ingestion (status, event_type, is_replay, latency_ms).
- **Frontend**:
  - Add **optimistic UI + local pending queue** keyed by client-generated intent ID to prevent double-tap duplicates and show pending state.
- **Ops/Onboarding**:
  - Update `.env.example`, README, and deployment checks to require `LINE_CHANNEL_SECRET` and explain idempotency behavior.

---

### 3. Implementation (concrete, minimal diffs)

#### 3.1. Ensure project path
```bash
cd /opt/axentx/workio/server
```

#### 3.2. Idempotency table (migration)
Append to `server/src/db/schema.sql` (or create migration):
```sql
-- Idempotency keys for LINE webhook events
CREATE TABLE IF NOT EXISTS line_event_idempotency (
  line_event_id VARCHAR(255) PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  processed_at TIMESTAMPTZ,
  event_type VARCHAR(50) NOT NULL,
  payload_hash TEXT
);

-- Retain 7 days; auto-clean via cron or TTL policy
CREATE INDEX IF NOT EXISTS idx_line_event_idempotency_created_at
ON line_event_idempotency (created_at);
```

#### 3.3. Signature verification middleware
Create `server/src/middleware/verifyLineSignature.ts`:
```ts
import crypto from 'crypto';
import type { Request, Response, NextFunction } from 'express';

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET;

export function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  if (!LINE_CHANNEL_SECRET) {
    console.warn('LINE_CHANNEL_SECRET missing — signature verification disabled (INSECURE)');
    return next();
  }

  const signature = req.headers['x-line-signature'] as string | undefined;
  if (!signature) {
    return res.status(401).json({ error: 'Missing X-Line-Signature' });
  }

  const body = JSON.stringify(req.body);
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body, 'utf8')
    .digest('base64');

  // Constant-time compare
  const expectedBuf = Buffer.from(expected);
  const receivedBuf = Buffer.from(signature);
  if (expectedBuf.length !== receivedBuf.length || !crypto.timingSafeEqual(expectedBuf, receivedBuf)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  next();
}
```

#### 3.4. Idempotency helper (pool-aware)
Create `server/src/lib/lineIdempotency.ts`:
```ts
import { Pool } from 'pg';

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

export async function isLineEventProcessed(lineEventId: string): Promise<boolean> {
  const client = await pool.connect();
  try {
    const { rows } = await client.query(
      'SELECT 1 FROM line_event_idempotency WHERE line_event_id = $1',
      [lineEventId]
    );
    return rows.length > 0;
  } finally {
    client.release();
  }
}

export async function markLineEventProcessed(
  lineEventId: string,
  eventType: string,
  payloadHash?: string
): Promise<boolean> {
  const client = await pool.connect();
  try {
    const { rowCount } = await client.query(
      `INSERT INTO line_event_idempotency (line_event_id, event_type, payload_hash, processed_at)
       VALUES ($1, $2, $3, NOW())
       ON CONFLICT (line_event_id) DO NOTHING`,
      [lineEventId, eventType, payloadHash || null]
    );
    return rowCount === 1; // true if newly inserted
  } finally {
    client.release();
  }
}
```

#### 3.5. Webhook route with validation + logging
Update/create `server/src/routes/webhook/line.ts`:
```ts
import express from 'express';
import crypto from 'crypto';
import { verifyLineSignature } from '../../middleware/verifyLineSignature';
import { isLineEventProcessed, markLineEventProcessed } from '../../lib/lineIdempotency';
import { validateClockEvent, applyClockEvent } from '../../services/clockService';
import { applyLeaveOrOt } from '../../services/leaveOtService';
import { logger, metrics } from '../../lib/observability';

const router = express.Router();

router.post('/line', verifyLineSignature, async (req, res) => {
  const startedAt = Date.now();
  const events = req.body.events;
  if (!Array.isArray(events) || events.length === 0) {
    metrics.increment('webhook.line.invalid_payload');
    return res.status(400).json({ error: 'Invalid payload' });
  }

  let processed = 0;
  let duplicates = 0;
  let errors = 0;

  for (const event of events) {
    // Stable idempotency key: prefer LINE's id if present, else deterministic fallback
    const lineEventId =
      event.source?.userId && event.timestamp
        ? `line:${event.source.userId}:${event.type}:${event.timestamp}`
        : event.webhookEventId ||
          event['webhookEventId'] ||
          crypto.createHash('sha256').update(JSON.stringify(event)).digest('hex');

    if (await isLineEventProcessed(lineEventId)) {
      duplicates++;
      continue; // safe to ACK to stop LINE retries
    }

    try {
      // Domain validation + business rules
      if (event.type === 'clock' || event.type === 'postback' /* clock payloads vary */) {
        const validation = validateClockEvent(event);
        if (!validation.ok) {
          logger.warn('Invalid clock event', { lineEventId, reason: validation.reason, event });
          metrics.increment('webhook.line.invalid_clock');
          continue; // ACK bad-but-non-retryable events to avoid LINE retry loops
        }
        await applyClockEvent(event);
      } else if (event.type === 'leave' || event.type === 'ot_request') {
        await applyLeaveOrOt(event);
      } else {
        logger.info('Unhandled LINE event type', { type: event.type, lineEventId });
      }

      const payloadHash = crypto.createHash('sha256').update(JSON.stringify(event)).digest('hex');
      await markLineEventProcessed(lineEventId, event.type, payloadHash);
      processed++;
    } catch (err) {
      errors++;
      logger.error('Error handling LINE event', { lineEventId, error: err, event });
      metrics.increment('webhook.line.processing_error');
      // Return non-2xx to trigger LINE retry for transient failures
      return res.status(500).json({ error: 'Processing failed' });
    }
  }

  const latencyMs = Date.now() - startedAt;
  metrics.increment('webhook.line.received', { count: events.length });
  metrics.increment('webhook.line.processed', { processed });
 
