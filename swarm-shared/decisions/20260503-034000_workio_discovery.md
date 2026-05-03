# workio / discovery

## 1. Diagnosis
- No observable telemetry for LINE webhook delivery/processing (silent failures, retries, or latency are invisible to operators).
- No structured request/response logging for `/webhook/line` (hard to debug signature mismatches or payload issues).
- Clock/leave/OT state changes from LINE rely on database polling or hard refresh in UI (no real-time push to frontend).
- Missing idempotency guard on LINE events (duplicate webhook deliveries can create duplicate clock/leave/OT records).
- No lightweight health/readiness probe for the LINE integration (deploy/rollback confidence is low).

## 2. Proposed change
Add a minimal, non-breaking observability + idempotency layer:
- File: `server/src/routes/line/webhook.ts` (main handler)
- File: `server/src/middleware/lineAuth.ts` (signature verification logging)
- File: `server/src/db/schema.sql` (add `line_webhook_events` table for idempotency + audit)
- File: `server/src/services/lineWebhookService.ts` (new, thin service to dedupe + emit events)
- File: `server/src/routes/line/health.ts` (lightweight probe)

## 3. Implementation

### 3.1 DB migration — schema.sql (append)
```sql
-- Idempotency + audit for LINE webhooks
CREATE TABLE IF NOT EXISTS line_webhook_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  line_event_id   TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  source_type     TEXT,
  source_id       TEXT,
  payload         JSONB NOT NULL,
  processed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(line_event_id)
);

-- Optional: index for fast lookup by event_id
CREATE INDEX IF NOT EXISTS idx_line_webhook_events_line_event_id
  ON line_webhook_events (line_event_id);
```

### 3.2 New service — lineWebhookService.ts
```ts
// server/src/services/lineWebhookService.ts
import { Pool } from 'pg';
import { logger } from '../utils/logger';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

export async function isEventProcessed(lineEventId: string): Promise<boolean> {
  const { rowCount } = await pool.query(
    'SELECT 1 FROM line_webhook_events WHERE line_event_id = $1',
    [lineEventId]
  );
  return Boolean(rowCount && rowCount > 0);
}

export async function markEventProcessed(
  lineEventId: string,
  eventType: string,
  sourceType: string | null,
  sourceId: string | null,
  payload: any
) {
  await pool.query(
    `INSERT INTO line_webhook_events (line_event_id, event_type, source_type, source_id, payload, processed_at)
     VALUES ($1, $2, $3, $4, $5, now())`,
    [lineEventId, eventType, sourceType, sourceId, JSON.stringify(payload)]
  );
}

export async function logWebhookReceived(lineEventId: string, eventType: string, payload: any) {
  logger.info({ lineEventId, eventType }, 'LINE webhook received');
}
```

### 3.3 Middleware — lineAuth.ts (add structured log + timing)
```ts
// server/src/middleware/lineAuth.ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { logger } from '../utils/logger';

export function lineAuth(req: Request, res: Response, next: NextFunction) {
  const channelSecret = process.env.LINE_CHANNEL_SECRET;
  const signature = req.headers['x-line-signature'] as string;
  if (!signature || !channelSecret) {
    logger.warn({ hasSignature: !!signature, hasSecret: !!channelSecret }, 'LINE auth missing');
    return res.status(401).json({ error: 'Unauthorized' });
  }

  const body = JSON.stringify(req.body);
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(body)
    .digest('base64');

  const ok = crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  logger.info({ ok, eventCount: req.body?.events?.length }, 'LINE webhook signature check');
  if (!ok) return res.status(401).json({ error: 'Invalid signature' });
  next();
}
```

### 3.4 Handler — webhook.ts (idempotency + structured log)
```ts
// server/src/routes/line/webhook.ts
import { Router } from 'express';
import { lineAuth } from '../middleware/lineAuth';
import { isEventProcessed, markEventProcessed, logWebhookReceived } from '../services/lineWebhookService';
import { logger } from '../utils/logger';

const router = Router();
router.use(lineAuth);

router.post('/webhook/line', async (req, res) => {
  const events = req.body?.events;
  if (!Array.isArray(events)) {
    logger.warn({ body: req.body }, 'Invalid LINE webhook payload');
    return res.status(400).json({ error: 'Invalid payload' });
  }

  const results = [];
  for (const ev of events) {
    const lineEventId = ev?.message?.id || ev?.source?.userId || `${Date.now()}-${Math.random()}`;
    const eventType = ev?.type || 'unknown';
    const sourceType = ev?.source?.type || null;
    const sourceId = ev?.source?.userId || ev?.source?.groupId || ev?.source?.roomId || null;

    try {
      logWebhookReceived(lineEventId, eventType, ev);

      if (await isEventProcessed(lineEventId)) {
        logger.info({ lineEventId }, 'Duplicate LINE event skipped');
        results.push({ lineEventId, status: 'duplicate' });
        continue;
      }

      // TODO: integrate with existing clock/leave/OT handlers here
      // Example: await handleClockIn(ev);

      await markEventProcessed(lineEventId, eventType, sourceType, sourceId, ev);
      results.push({ lineEventId, status: 'processed' });
    } catch (err) {
      logger.error({ err, lineEventId, eventType }, 'LINE webhook processing failed');
      results.push({ lineEventId, status: 'error', error: String(err) });
    }
  }

  // Always 200 to prevent LINE retries for non-auth errors
  res.json({ ok: true, results });
});

export default router;
```

### 3.5 Health probe — health.ts
```ts
// server/src/routes/line/health.ts
import { Router } from 'express';
import { Pool } from 'pg';

const router = Router();
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

router.get('/health/line', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok', line: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'error', line: 'db unavailable' });
  }
});

export default router;
```

### 3.6 Wire up (add to main app)
```ts
// server/src/app.ts (or index.ts) — add imports and routes
import lineWebhookRouter from './routes/line/webhook';
import lineHealthRouter from './routes/line/health';

app.use('/webhook', lineWebhookRouter);
app.use('/health', lineHealthRouter);
```

### 3.7 Logger helper (if missing)
```ts
// server/src/utils/logger.ts
import pino from 'pino';
export const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  transport: process.env.NODE_ENV !== 'production' ? { target: 'pino-pretty' } : undefined,
});
```

## 4. Verification
- Run migration: `psql workio < server/src/db/schema.sql`
- Start backend: `npm run dev`
- Send a test LINE event (use ngrok + LINE console or `curl` with a valid signature):
  ```bash
  curl -X POST http://localhost:3000/webhook/line \
    -H "Content-Type: application/json" \
    -H "X-Line-S
