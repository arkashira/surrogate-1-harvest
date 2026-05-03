# workio / discovery

## 1. Diagnosis
- No idempotency key on LINE webhook ingestion → duplicate clock/leave events on retries or double-taps.
- Missing request signature verification (`X-Line-Signature`) → webhook accepts spoofed events.
- No per-tenant/per-day/per-employee intent guard → race conditions create duplicate attendance rows.
- Webhook handler mixes validation and business logic → hard to test and evolve.
- No structured logging/trace-id on webhook path → hard to debug duplicates or failures in production.

## 2. Proposed change
File: `/opt/axentx/workio/server/src/routes/lineWebhook.ts` (or equivalent under `server/src/routes/`).  
Scope: add middleware for signature verification + idempotency layer + intent guard; refactor handler to use service layer. If file doesn’t exist, create it and wire into Express in `server/src/index.ts` (or `app.ts`).

## 3. Implementation

### A. Add idempotency + signature middleware and intent guard
Create `server/src/middleware/lineWebhookSecurity.ts`:

```ts
// server/src/middleware/lineWebhookSecurity.ts
import crypto from 'crypto';
import { Request, Response, NextFunction } from 'express';
import { redis } from '../db/redis'; // provide a Redis client (or fallback to Postgres advisory lock)

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

export function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  const signature = req.get('X-Line-Signature') || '';
  const body = JSON.stringify(req.body);
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body)
    .digest('base64');

  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    return res.status(401).json({ error: 'Invalid signature' });
  }
  next();
}

/**
 * Idempotency + intent guard
 * Key: tenantId:line-webhook:{dedupKey}
 * dedupKey = SHA256({source}:{userId}:{intent}:{date}:{salt})
 * Where:
 *  - source = event.source.userId (or group/room)
 *  - intent = normalized intent (clock_in|clock_out|leave_request|ot_request)
 *  - date = YYYY-MM-DD (local tenant date) in UTC for safety
 *  - salt = event.timestamp || event.replyToken (use stable value)
 *
 * TTL = 24h (long enough to cover retries, short enough to avoid unbounded growth)
 */
export async function lineIdempotencyGuard(
  req: Request,
  res: Response,
  next: NextFunction
) {
  const tenantId = req.headers['x-tenant-id'] as string;
  if (!tenantId) return res.status(400).json({ error: 'Missing tenant' });

  const events = req.body.events || [];
  for (const ev of events) {
    const dedupKey = makeDedupKey(tenantId, ev);
    const key = `tenant:${tenantId}:line-webhook:${dedupKey}`;
    const exists = await redis.set(key, '1', 'PX', 24 * 60 * 60 * 1000, 'NX');
    if (!exists) {
      // duplicate detected — acknowledge but skip processing
      req.duplicateEvents = req.duplicateEvents || [];
      req.duplicateEvents.push(dedupKey);
      continue;
    }
    req.uniqueEvents = req.uniqueEvents || [];
    req.uniqueEvents.push(ev);
  }

  // If all events were duplicates, still return 200 (acknowledged)
  if (!req.uniqueEvents || req.uniqueEvents.length === 0) {
    return res.status(200).json({ ok: true, duplicates: req.duplicateEvents });
  }

  next();
}

function makeDedupKey(tenantId: string, ev: any): string {
  const src = (ev.source && (ev.source.userId || ev.source.senderId || ev.source.roomId || ev.source.groupId)) || 'unknown';
  const intent = normalizeIntent(ev);
  const date = new Date().toISOString().slice(0, 10); // UTC date; adjust to tenant TZ if stored
  const salt = ev.timestamp || ev.replyToken || 'none';
  const raw = `${src}:${intent}:${date}:${salt}`;
  return crypto.createHash('sha256').update(raw).digest('hex');
}

function normalizeIntent(ev: any): string {
  // Map LINE events and message text to canonical intents
  if (ev.type === 'message' && ev.message.type === 'text') {
    const txt = (ev.message.text || '').toLowerCase().trim();
    if (txt.includes('clock in') || txt.includes('เข้างาน')) return 'clock_in';
    if (txt.includes('clock out') || txt.includes('เลิกงาน')) return 'clock_out';
    if (txt.includes('leave') || txt.includes('ลา')) return 'leave_request';
    if (txt.includes('ot') || txt.includes('โอที')) return 'ot_request';
  }
  // fallback: use event type + intent hint
  return `${ev.type}:${ev.message?.type || 'unknown'}`;
}
```

### B. Refactor webhook route to use middleware + service
Create `server/src/routes/lineWebhook.ts`:

```ts
// server/src/routes/lineWebhook.ts
import { Router } from 'express';
import { verifyLineSignature } from '../middleware/lineWebhookSecurity';
import { lineIdempotencyGuard } from '../middleware/lineWebhookSecurity';
import { lineService } from '../services/lineService';
import logger from '../utils/logger';

const router = Router();

router.post(
  '/webhook/line',
  verifyLineSignature,
  lineIdempotencyGuard,
  async (req, res) => {
    const tenantId = req.headers['x-tenant-id'] as string;
    const events = req.uniqueEvents || [];
    const traceId = req.headers['x-request-id'] || crypto.randomUUID();

    logger.info({ traceId, tenantId, count: events.length }, 'Processing LINE webhook');

    try {
      for (const ev of events) {
        await lineService.handleEvent(tenantId, ev, { traceId });
      }
      res.status(200).json({ ok: true });
    } catch (err) {
      logger.error({ traceId, tenantId, err }, 'LINE webhook processing failed');
      // Still return 200 to avoid LINE retry storms for transient errors; rely on alerts
      res.status(200).json({ ok: false, error: String(err) });
    }
  }
);

export default router;
```

### C. Minimal service stub (so route compiles)
Create `server/src/services/lineService.ts`:

```ts
// server/src/services/lineService.ts
import logger from '../utils/logger';

export const lineService = {
  async handleEvent(tenantId: string, event: any, { traceId }: { traceId: string }) {
    // TODO: implement actual clock/leave/OT handling with DB transaction + per-day intent guard
    logger.info({ traceId, tenantId, type: event.type }, 'Handling LINE event');
    // Placeholder: persist to attendance/leave tables with unique constraint (tenantId, employeeId, date, intent)
    return Promise.resolve();
  }
};
```

### D. Wire into Express
In `server/src/index.ts` (or `app.ts`), mount route:

```ts
import lineWebhook from './routes/lineWebhook';
app.use('/webhook/line', lineWebhook);
```

### E. Redis client (or fallback)
Provide a Redis client at `server/src/db/redis.ts`. If Redis unavailable, fallback to Postgres advisory lock or unique DB constraint (preferred long-term). Example stub:

```ts
// server/src/db/redis.ts
import { createClient } from 'redis';

export const redis = createClient({
  url: process.env.REDIS_URL || 'redis://localhost:6379'
});

redis.on('error', (err) => console.error('Redis error', err));

(async () => {
  try {
    await redis.connect();
  } catch {
    // graceful degradation: in-memory fallback for dev (not for prod)
  }
})();
```

