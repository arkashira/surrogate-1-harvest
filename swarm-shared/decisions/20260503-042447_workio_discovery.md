# workio / discovery

## 1. Diagnosis
- Missing `X-Line-Signature` verification on `/webhook/line` allows spoofed clock-in/out events → attendance fraud and phantom records.
- No idempotency guard on LINE webhook ingestion; LINE retries on 5xx/timeouts → duplicate clock/leave/OT records on replay.
- No replay-window or timestamp tolerance check → events can be captured and replayed later to backdate attendance.
- No structured audit trail for webhook receipt vs. processing outcome → hard to debug duplicates or fraud.
- No early rejection for unverified payloads before DB writes → wasted cycles and log noise.

## 2. Proposed change
File: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (or create if absent)  
Scope: add middleware that (a) verifies `X-Line-Signature`, (b) checks `timestamp` within ±5 min, (c) deduplicates by `X-Line-Delivery` or event `webhookEventId`, (d) logs receipt+verification result, then passes to handlers.

## 3. Implementation
```bash
# Ensure dependencies
cd /opt/axentx/workio/server
npm install crypto
```

Create/replace `/opt/axentx/workio/server/src/routes/webhook/line.ts`:
```ts
import express, { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { db } from '../../db';
import { logger } from '../../utils/logger';

const router = express.Router();

// -- Helpers --
function verifyLineSignature(rawBody: string, signature: string, channelSecret: string): boolean {
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function isTimestampFresh(ts: string, toleranceSec = 300): boolean {
  const now = Date.now();
  const evt = new Date(ts).getTime();
  return Math.abs(now - evt) <= toleranceSec * 1000;
}

// -- Dedupe store (in-memory for single instance; use Redis/DB for multi-instance) --
const seenDeliveries = new Set<string>();
function isDuplicate(deliveryId: string): boolean {
  if (seenDeliveries.has(deliveryId)) return true;
  seenDeliveries.add(deliveryId);
  // optional: prune after e.g. 24h if long-lived process
  return false;
}

// -- Middleware --
function lineWebhookGuard(req: Request, res: Response, next: NextFunction) {
  const channelSecret = process.env.LINE_CHANNEL_SECRET;
  const signature = req.headers['x-line-signature'] as string;
  const deliveryId = req.headers['x-line-delivery'] as string;

  if (!channelSecret || !signature) {
    logger.warn({ msg: 'webhook/line: missing secret or signature' });
    return res.status(401).json({ error: 'Unauthorized' });
  }

  // Raw body required for signature verification
  const rawBody = (req as any).rawBody || JSON.stringify(req.body);
  if (!rawBody) {
    logger.warn({ msg: 'webhook/line: missing rawBody' });
    return res.status(400).json({ error: 'Bad Request' });
  }

  if (!verifyLineSignature(rawBody, signature, channelSecret)) {
    logger.warn({ msg: 'webhook/line: invalid signature', deliveryId });
    return res.status(401).json({ error: 'Invalid signature' });
  }

  // Basic freshness check (events contain timestamp)
  const events = req.body?.events;
  if (Array.isArray(events) && events.length > 0) {
    const oldest = events.map((e: any) => e.timestamp).sort()[0];
    if (oldest && !isTimestampFresh(oldest)) {
      logger.warn({ msg: 'webhook/line: stale timestamp', deliveryId, oldest });
      return res.status(400).json({ error: 'Stale event' });
    }
  }

  // Idempotency by delivery id
  if (deliveryId && isDuplicate(deliveryId)) {
    logger.info({ msg: 'webhook/line: duplicate delivery', deliveryId });
    return res.status(200).json({ ok: true }); // acknowledge duplicate
  }

  // Attach metadata for handlers
  (req as any).lineMeta = { deliveryId, verifiedAt: Date.now() };
  next();
}

// -- Audit log helper --
async function logWebhookReceipt(deliveryId: string, events: any[], result: string) {
  try {
    await db.query(
      `INSERT INTO webhook_audit (delivery_id, event_count, result, received_at)
       VALUES ($1, $2, $3, NOW())`,
      [deliveryId, events?.length || 0, result]
    );
  } catch (err) {
    logger.error({ msg: 'webhook/line: audit insert failed', err });
  }
}

// -- Routes --
router.post(
  '/webhook/line',
  // Middleware to capture raw body (must be placed before json parser in app setup)
  // This route assumes app uses express.json({ verify: (req, res, buf) => { (req as any).rawBody = buf.toString(); } })
  lineWebhookGuard,
  async (req: Request, res: Response) => {
    const meta = (req as any).lineMeta;
    const events = req.body?.events || [];

    try {
      // Process events (clock-in/out, leave, OT)
      for (const ev of events) {
        // Example handler shape — adapt to your domain
        const { type, source, message, timestamp } = ev;
        if (type === 'message' && message?.type === 'text') {
          const text = message.text.trim().toLowerCase();
          const userId = source.userId;
          // TODO: map userId -> employee, validate shift rules, GPS if provided in metadata
          if (text === 'clock in') {
            // idempotent upsert by userId+date+shift
            await db.query(
              `INSERT INTO attendance (employee_id, clock_in, date, source, line_delivery_id)
               VALUES ($1, NOW(), CURRENT_DATE, $2, $3)
               ON CONFLICT (employee_id, date) DO UPDATE
               SET clock_in = EXCLUDED.clock_in, updated_at = NOW()`,
              [userId, 'line', meta.deliveryId]
            );
          } else if (text === 'clock out') {
            await db.query(
              `UPDATE attendance
               SET clock_out = NOW(), updated_at = NOW()
               WHERE employee_id = $1 AND date = CURRENT_DATE AND clock_out IS NULL`,
              [userId]
            );
          }
          // leave/OT: create request row with status pending
        }
      }

      await logWebhookReceipt(meta.deliveryId, events, 'processed');
      res.status(200).json({ ok: true });
    } catch (err) {
      await logWebhookReceipt(meta.deliveryId, events, 'error');
      logger.error({ msg: 'webhook/line: processing failed', err, deliveryId: meta.deliveryId });
      // Return 500 so LINE will retry (if transient); for validation errors use 4xx
      res.status(500).json({ error: 'Processing failed' });
    }
  }
);

export default router;
```

Wire into your main app (e.g., `/opt/axentx/workio/server/src/app.ts`):
```ts
import express from 'express';
import lineWebhookRouter from './routes/webhook/line';

const app = express();

// IMPORTANT: capture raw body for signature verification
app.use(
  '/webhook/line',
  express.json({
    verify: (req: any, res, buf) => {
      req.rawBody = buf.toString();
    },
  })
);

app.use(lineWebhookRouter);

// ... other routes and error handling
export default app;
```

Add audit table (run once):
```sql
-- server/src/db/schema.sql addition
CREATE TABLE IF NOT EXISTS webhook_audit (
  id SERIAL PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  event_count INT NOT NULL,
  result TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ
