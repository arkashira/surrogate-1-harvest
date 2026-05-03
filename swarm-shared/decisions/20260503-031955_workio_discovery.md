# workio / discovery

## 1. Diagnosis

- Missing **LINE webhook signature verification** (`X-Line-Signature`) on `/webhook/line` allows spoofed/replayed events to be accepted as valid clock-in/out or leave requests.
- No **idempotency/replay protection** — LINE retries on 5xx/timeouts can create duplicate `attendance_punches`, `leave_requests`, `ot_requests`.
- No **request deduplication key** (e.g., `line_event_id` or idempotency token) stored at DB layer to detect and reject replays.
- Webhook handler likely accepts raw body without buffering/verification before parsing, making signature checks unreliable.
- No defense against **timestamp skew/replay window** — old events could be replayed beyond a reasonable validity period.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/routes/lineWebhook.ts` (or equivalent route handling `/webhook/line`)  
Scope:
- Add `X-Line-Signature` verification using HMAC-SHA256 with channel secret.
- Extract and store `line_event_id` (or use `deliveryId`/`webhookEventId`) as unique constraint in relevant tables.
- Add idempotency check before creating punches/leave/OT records.
- Reject events older than 5 minutes (replay window).

## 3. Implementation

```bash
# Ensure route file exists; create if missing
touch /opt/axentx/workio/server/src/routes/lineWebhook.ts
```

```ts
// /opt/axentx/workio/server/src/routes/lineWebhook.ts
import express, { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { body, validationResult } from 'express-validator';
import db from '../db';
import { verifyLineSignature, isReplayEvent } from '../utils/lineSecurity';

const router = express.Router();

// Buffer raw body for signature verification
router.use(
  express.raw({ type: 'application/json' }),
  (req: Request, res: Response, next: NextFunction) => {
    (req as any).rawBody = req.body;
    next();
  }
);

router.post(
  '/webhook/line',
  body().isObject(),
  async (req: Request, res: Response) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ error: 'Invalid payload' });
    }

    const channelSecret = process.env.LINE_CHANNEL_SECRET;
    const signature = req.get('X-Line-Signature');
    const rawBody = (req as any).rawBody as Buffer;

    if (!signature || !channelSecret) {
      return res.status(401).json({ error: 'Missing signature or secret' });
    }

    if (!verifyLineSignature(channelSecret, rawBody, signature)) {
      return res.status(401).json({ error: 'Invalid signature' });
    }

    const events = req.body.events || [];

    for (const event of events) {
      // Replay protection
      if (isReplayEvent(event)) {
        continue; // skip old events
      }

      // Idempotency check
      const exists = await db.oneOrNone(
        'SELECT 1 FROM line_events WHERE line_event_id = $1',
        [event.source?.userId + ':' + event.timestamp + ':' + event.type]
      );
      if (exists) {
        continue; // already processed
      }

      // Record event to prevent replays
      await db.none(
        `INSERT INTO line_events (line_event_id, event_type, payload, created_at)
         VALUES ($1, $2, $3, NOW())`,
        [
          event.source?.userId + ':' + event.timestamp + ':' + event.type,
          event.type,
          JSON.stringify(event),
        ]
      );

      // Route to handlers
      await handleLineEvent(event);
    }

    return res.status(200).json({ status: 'ok' });
  }
);

async function handleLineEvent(event: any) {
  // Minimal dispatcher — expand per domain needs
  switch (event.type) {
    case 'message':
      // handle clock-in/out via message
      break;
    case 'postback':
      // handle leave/OT requests
      break;
    default:
      break;
  }
}

export default router;
```

```ts
// /opt/axentx/workio/server/src/utils/lineSecurity.ts
import crypto from 'crypto';

export function verifyLineSignature(
  channelSecret: string,
  body: Buffer,
  signature: string
): boolean {
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(body)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

const REPLAY_WINDOW_MS = 5 * 60 * 1000; // 5 minutes

export function isReplayEvent(event: any): boolean {
  const eventTime = event.timestamp;
  if (!eventTime) return false;
  const now = Date.now();
  return Math.abs(now - eventTime) > REPLAY_WINDOW_MS;
}
```

```sql
-- /opt/axentx/workio/server/src/db/schema.sql additions
-- Idempotency table for LINE events
CREATE TABLE IF NOT EXISTS line_events (
  id              SERIAL PRIMARY KEY,
  line_event_id   TEXT NOT NULL UNIQUE,
  event_type      TEXT NOT NULL,
  payload         JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Optional: add unique constraint on attendance_punches for same user+time window
ALTER TABLE attendance_punches
  ADD CONSTRAINT unique_punch_per_window
  UNIQUE (user_id, tenant_id, punch_time);
```

```diff
# /opt/axentx/workio/server/src/app.ts (or main server file)
+ import lineWebhook from './routes/lineWebhook';
+ app.use('/webhook/line', lineWebhook);
```

## 4. Verification

1. **Signature verification**
   - Start server: `npm run dev`
   - Send test POST with valid/invalid `X-Line-Signature`:
     ```bash
     curl -X POST http://localhost:3000/webhook/line \
       -H "Content-Type: application/json" \
       -H "X-Line-Signature: invalidsig" \
       -d '{"events":[]}'
     ```
   - Expect `401` for invalid sig, `200` for valid (when using real secret + properly signed body).

2. **Idempotency / replay protection**
   - Send same event payload twice (same `line_event_id` logic).
   - Check DB: only one row in `line_events` and no duplicate `attendance_punches`.

3. **Replay window**
   - Send event with `timestamp` older than 5 minutes.
   - Confirm it is skipped (no DB insert, no punch created).

4. **End-to-end via LINE (manual)**
   - Configure webhook in LINE OA to point to your dev tunnel (e.g., ngrok).
   - Clock in/out from LINE app.
   - Verify punch created once and no duplicates on LINE retries (simulate by returning 5xx once).
