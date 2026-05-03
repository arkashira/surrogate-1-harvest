# workio / discovery

## 1. Diagnosis

- Missing **LINE webhook signature verification** (`X-Line-Signature`) on `/webhook/line` allows spoofed/replayed events to be accepted as valid clock-in/out or leave requests.
- No **idempotency/replay protection** — LINE retries on 5xx/timeouts can create duplicate `clock_records`, `leave_requests`, or `ot_requests` for the same `line_event_id`.
- Clock-in/out UX has no optimistic state or local pending queue; users can double-tap and trigger duplicate punches while waiting on webhook round-trip.
- Webhook handler does not validate critical payload fields (e.g., `userId`, `type`, `timestamp`) before side-effects, risking malformed state.
- No lightweight observability (request-id, timing, outcome) on the webhook path to debug delivery or replay issues.

## 2. Proposed change

- **File**: `workio/server/src/routes/webhook/line.ts` (create if absent) or existing webhook handler.
- **Scope**: Add middleware + handler for `POST /webhook/line` that:
  - Verifies `X-Line-Signature` using HMAC-SHA256 with `CHANNEL_SECRET`.
  - Enforces idempotency on `event.id` (stored as `line_event_id` in relevant tables with unique constraint).
  - Normalizes and validates minimal payload fields before dispatching to domain actions.
  - Returns `200` quickly (async processing) and logs request-id + outcome.

## 3. Implementation

```bash
# Ensure dependencies
cd /opt/axentx/workio/server
npm install crypto
```

```ts
// workio/server/src/routes/webhook/line.ts
import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { db } from '../db';
import { validateLineEvent, processLineEvent } from '../../services/lineEventService';

const router = Router();
const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

if (!CHANNEL_SECRET) {
  console.warn('LINE_CHANNEL_SECRET missing — webhook verification disabled (dev only)');
}

function verifyLineSignature(rawBody: string | Buffer, signature: string): boolean {
  if (!CHANNEL_SECRET) return true; // allow in dev
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

// Idempotency check helper
async function isDuplicateLineEvent(lineEventId: string): Promise<boolean> {
  // Check across relevant tables where we store line_event_id
  const tables = ['clock_records', 'leave_requests', 'ot_requests', 'line_events'];
  for (const table of tables) {
    const result = await db.oneOrNone(
      `SELECT 1 FROM ${table} WHERE line_event_id = $1 LIMIT 1`,
      [lineEventId]
    );
    if (result) return true;
  }
  return false;
}

router.post(
  '/line',
  async (req: Request, res: Response, next: NextFunction) => {
    const requestId = `line-webhook-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    const signature = req.headers['x-line-signature'] as string;
    const rawBody = JSON.stringify(req.body); // raw string must match exactly what LINE sent

    // Basic validation
    if (!signature) {
      console.warn(`[${requestId}] Missing X-Line-Signature`);
      return res.status(400).json({ error: 'Missing signature' });
    }

    if (!verifyLineSignature(rawBody, signature)) {
      console.warn(`[${requestId}] Invalid LINE signature`);
      return res.status(401).json({ error: 'Invalid signature' });
    }

    const events = req.body?.events;
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(200).json({ ok: true }); // LINE expects 200 even if empty
    }

    // Quick duplicate check for each event (best-effort fast path)
    const duplicates = await Promise.all(
      events.map((e: any) => (e?.id ? isDuplicateLineEvent(e.id) : Promise.resolve(false)))
    );

    // Accept and process non-duplicates asynchronously
    // Respond 200 immediately to stop LINE retries
    res.status(200).json({ ok: true });

    // Async processing (fire-and-forget but with logging)
    (async () => {
      for (let i = 0; i < events.length; i++) {
        const event = events[i];
        const lineEventId = event?.id;
        if (!lineEventId) continue;
        if (duplicates[i]) {
          console.log(`[${requestId}] Duplicate event skipped: ${lineEventId}`);
          continue;
        }

        try {
          if (!validateLineEvent(event)) {
            console.warn(`[${requestId}] Invalid event shape: ${lineEventId}`, event);
            continue;
          }

          await processLineEvent(event, { requestId, lineEventId });
          console.log(`[${requestId}] Processed event: ${lineEventId}`);
        } catch (err) {
          console.error(`[${requestId}] Failed processing ${lineEventId}:`, err);
          // Note: we already returned 200; LINE won't retry. Consider DLQ/alerting here.
        }
      }
    })();
  }
);

export default router;
```

```ts
// workio/server/src/services/lineEventService.ts
import { db } from './db';

export function validateLineEvent(event: any): boolean {
  if (!event || typeof event !== 'object') return false;
  if (!event.id || typeof event.id !== 'string') return false;
  if (!event.type || typeof event.type !== 'string') return false;
  if (!event.timestamp || typeof event.timestamp !== 'number') return false;
  if (!event.source || !event.source.userId) return false;
  return true;
}

export async function processLineEvent(event: any, meta: { requestId: string; lineEventId: string }) {
  const { type, source, message, postback } = event;
  const userId = source.userId;

  // Record raw event for audit/idempotency
  await db.none(
    `INSERT INTO line_events (line_event_id, type, user_id, payload, created_at)
     VALUES ($1, $2, $3, $4, NOW())`,
    [meta.lineEventId, type, userId, JSON.stringify(event)]
  );

  // Dispatch to domain handlers
  if (type === 'message' && message?.type === 'text') {
    const text = (message.text || '').trim().toLowerCase();
    if (text === 'clockin' || text === 'clockout') {
      await handleClockAction(userId, text === 'clockin' ? 'in' : 'out', meta);
    }
  } else if (type === 'postback') {
    // handle leave/OT postback payloads here
    const data = postback?.data;
    // parse and route to domain actions
  }
}

async function handleClockAction(userId: string, direction: 'in' | 'out', meta: { requestId: string; lineEventId: string }) {
  // Example: insert clock record with line_event_id for idempotency
  await db.none(
    `INSERT INTO clock_records (user_id, direction, line_event_id, created_at)
     VALUES ($1, $2, $3, NOW())`,
    [userId, direction, meta.lineEventId]
  );
}
```

```sql
-- workio/server/src/db/schema.sql additions (run once)
-- Ensure line_events table exists for audit/idempotency
CREATE TABLE IF NOT EXISTS line_events (
  id SERIAL PRIMARY KEY,
  line_event_id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  user_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

-- Add line_event_id to critical tables if not present (idempotency)
ALTER TABLE clock_records ADD COLUMN IF NOT EXISTS line_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_clock_records_line_event_id ON clock_records (line_event_id) WHERE line_event_id IS NOT NULL;

ALTER TABLE leave_requests ADD COLUMN
