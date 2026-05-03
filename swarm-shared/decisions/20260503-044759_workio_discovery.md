# workio / discovery

## 1. Diagnosis

- **No idempotency guard on LINE webhook ingestion**: duplicate `X-Line-Signature` replays (network retries, client double-taps) can double-apply clock-in/out and leave/OT transitions, corrupting daily totals and approval state.
- **Missing strict `X-Line-Signature` verification**: handler processes payload before HMAC validation, enabling spoofed or tampered events to mutate attendance records.
- **No replay-window protection**: same `webhookEventId` (or timestamp+userId) within minutes is accepted multiple times; no dedupe table or cache to short-circuit duplicates.
- **Partial rollback on failure**: if clock-in succeeds but downstream notification/DB constraint fails, state is left inconsistent (punch recorded but no audit trail or compensating event).
- **No observability for webhook replays**: missing request-id propagation and idempotency-key logging makes incidents hard to trace and reproduce.

## 2. Proposed change

- **File**: `workio/server/src/routes/line/webhook.ts` (or equivalent webhook handler)
- **Scope**: add idempotency middleware + strict signature verification + replay dedupe table and cache (Redis or DB) keyed by `X-Line-Signature` + `nonce` or `webhookEventId`.

## 3. Implementation

```bash
# Ensure dependencies
cd /opt/axentx/workio/server
npm install crypto redis
```

```ts
// workio/server/src/middleware/lineSignature.ts
import crypto from 'crypto';

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

export function verifyLineSignature(rawBody: string, signature: string): boolean {
  if (!LINE_CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');
  // constant-time compare to avoid timing attacks
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}
```

```ts
// workio/server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';
import { verifyLineSignature } from './lineSignature';

// Simple in-memory store for demo; replace with Redis/DB in prod
const seen = new Map<string, { ts: number; status: number }>();
const TTL_MS = 5 * 60 * 1000; // 5m replay window

export function lineIdempotency(req: Request, res: Response, next: NextFunction) {
  const signature = req.get('X-Line-Signature') || '';
  const rawBody = (req as any).rawBody || JSON.stringify(req.body);
  if (!rawBody) return res.status(400).json({ error: 'missing body' });

  // 1) Strict signature verification first
  if (!verifyLineSignature(rawBody, signature)) {
    return res.status(401).json({ error: 'invalid signature' });
  }

  // 2) Idempotency key: prefer event id, else hash signature+body
  let eventId: string | undefined;
  try {
    const payload = JSON.parse(rawBody);
    eventId = payload?.events?.[0]?.webhookEventId || payload?.events?.[0]?.replyToken;
  } catch {
    // ignore
  }
  const key = eventId || crypto.createHash('sha256').update(signature + rawBody).digest('hex');

  const now = Date.now();
  const existing = seen.get(key);
  if (existing && now - existing.ts < TTL_MS) {
    // Replay detected — return original status without side-effects
    return res.status(existing.status).json({ replay: true, message: 'duplicate event ignored' });
  }

  // Attach key to request for downstream commit
  (req as any).idempotencyKey = key;

  // Wrap res.json to capture final status for dedupe store
  const originalJson = res.json.bind(res);
  res.json = function (body) {
    seen.set(key, { ts: now, status: res.statusCode || 200 });
    return originalJson(body);
  };

  next();
}
```

```ts
// workio/server/src/routes/line/webhook.ts
import express from 'express';
import { lineIdempotency } from '../../middleware/idempotency';
import { handleLineEvent } from '../../services/lineService';

const router = express.Router();

// IMPORTANT: preserve raw body for signature verification
router.post(
  '/webhook/line',
  express.raw({ type: 'application/json' }),
  lineIdempotency,
  async (req, res) => {
    try {
      const rawBody = (req as any).rawBody?.toString?.() || '{}';
      const payload = JSON.parse(rawBody);
      await handleLineEvent(payload, (req as any).idempotencyKey);
      res.json({ ok: true });
    } catch (err) {
      console.error('LINE webhook error', err);
      res.status(500).json({ error: 'processing failed' });
    }
  }
);

export default router;
```

```ts
// workio/server/src/services/lineService.ts
import { Pool } from 'pg';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

export async function handleLineEvent(payload: any, idempotencyKey: string) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check at DB level (stronger guarantee)
    const exists = await client.query(
      `SELECT 1 FROM attendance_events WHERE idempotency_key = $1 LIMIT 1`,
      [idempotencyKey]
    );
    if (exists.rows.length) {
      await client.query('ROLLBACK');
      return; // already processed
    }

    for (const event of payload.events || []) {
      const { type, source, message, postback } = event;
      const userId = source?.userId;
      const ts = event.timestamp ? new Date(event.timestamp) : new Date();

      if (type === 'message' && message?.text === 'clock in') {
        await client.query(
          `INSERT INTO attendance_events (user_id, event_type, ts, idempotency_key)
           VALUES ($1, $2, $3, $4)`,
          [userId, 'clock_in', ts, idempotencyKey]
        );
      } else if (type === 'message' && message?.text === 'clock out') {
        await client.query(
          `INSERT INTO attendance_events (user_id, event_type, ts, idempotency_key)
           VALUES ($1, $2, $3, $4)`,
          [userId, 'clock_out', ts, idempotencyKey]
        );
      }
      // leave/OT handling omitted for brevity — same pattern
    }

    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

```sql
-- workio/server/src/db/schema.sql additions
ALTER TABLE attendance_events ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_idempotency ON attendance_events (idempotency_key) WHERE idempotency_key IS NOT NULL;
```

## 4. Verification

1. **Signature verification**:
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: invalidsig" \
     -d '{"events":[]}'
   # Expect 401
   ```

2. **Idempotency (in-memory)**:
   - Send same valid payload twice within 5m; second request returns `200` with `{replay: true}` and no duplicate DB rows.
   - Check DB: `SELECT COUNT(*) FROM attendance_events WHERE idempotency_key = '<key>';` → must be 1.

3. **DB-level dedupe resilience**:
   - Simulate crash after first insert but before commit by forcing error in handler; retry same idempotency key → no duplicate row due to unique index.

4. **Replay after TTL**:
   - Wait >5m (or lower TTL for test), resend same payload → should process normally and create new event (or reject
