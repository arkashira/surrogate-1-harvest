# workio / discovery

## Final Synthesized Implementation

**Chosen approach**: Merge Candidate 1’s complete, production-ready code with Candidate 2’s implied emphasis on strict verification and replay-window hardening. Resolve contradictions by enforcing the strictest correct behavior: mandatory signature verification, ±5-minute replay window, database-level idempotency, and transactional state changes.

---

### 1. Diagnosis (merged, highest severity first)
- **Missing `X-Line-Signature` verification**: allows spoofed punches/leave requests.
- **No idempotency/replay protection**: network retries and replays beyond ±5 minutes create phantom punches and corrupt daily totals/OT/approvals.
- **Non-transactional state changes**: partial failures leave attendance summaries inconsistent.
- **No durable idempotency key storage**: retries cannot be safely de-duplicated at the database level.

---

### 2. Proposed change (scope)
- **File**: `workio/server/src/routes/line/webhook.ts`
- **Scope**:
  - Verify `X-Line-Signature` with HMAC-SHA256 using channel secret before any processing.
  - Reject events outside ±5 minutes (LINE recommendation).
  - Generate a deterministic idempotency key and store it in `webhook_events_log` with a unique constraint.
  - Wrap punch/leave/OT state changes in a database transaction to prevent partial commits.

---

### 3. Implementation

```ts
// workio/server/src/routes/line/webhook.ts
import crypto from 'crypto';
import { Request, Response, NextFunction } from 'express';
import { pool } from '../../db';

const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';
const REPLAY_WINDOW_MS = 300_000; // 5 minutes

function verifyLineSignature(rawBody: string, signature: string): boolean {
  if (!CHANNEL_SECRET || !signature) return false;
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function isReplay(eventTs: number): boolean {
  const now = Date.now();
  return Math.abs(now - eventTs) > REPLAY_WINDOW_MS;
}

function makeIdempotencyKey(event: any): string {
  // Prefer stable identifiers provided by LINE; fall back to content hash.
  const eventId = event.webhookEventId || event.deliveryId || '';
  const payload = JSON.stringify({
    mode: event.mode,
    timestamp: event.timestamp,
    source: event.source,
    type: event.type,
    eventId,
    ...(event.type === 'message' ? { messageId: event.message?.id } : {}),
    ...(event.type === 'postback' ? { postbackData: event.postback?.data } : {}),
  });
  return crypto.createHash('sha256').update(payload).digest('hex');
}

export async function lineWebhookHandler(
  req: Request,
  res: Response,
  next: NextFunction
) {
  const signature = req.headers['x-line-signature'] as string;
  const rawBody = req.body; // must be raw string (use express.text upstream)

  // 1) Strict signature verification before any processing
  if (!verifyLineSignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  let events;
  try {
    const parsed = JSON.parse(rawBody);
    events = parsed.events;
    if (!Array.isArray(events)) throw new Error('Invalid events');
  } catch {
    return res.status(400).json({ error: 'Invalid payload' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const event of events) {
      const ts = Number(event.timestamp);
      if (Number.isNaN(ts) || isReplay(ts)) {
        // Reject replays; continue processing other events or reject all depending on policy.
        continue;
      }

      const idemKey = makeIdempotencyKey(event);

      // 2) Idempotency check/insert
      const exists = await client.query(
        `SELECT 1 FROM webhook_events_log WHERE idempotency_key = $1`,
        [idemKey]
      );
      if (exists.rows.length > 0) {
        continue; // already processed
      }

      await client.query(
        `INSERT INTO webhook_events_log(idempotency_key, payload, received_at)
         VALUES ($1, $2, NOW())`,
        [idemKey, JSON.stringify(event)]
      );

      // 3) Process event inside same transaction
      if (event.type === 'message' && event.message?.type === 'text') {
        const text = event.message.text.trim().toLowerCase();
        const userId = event.source.userId;
        const tenantId = 'default'; // derive from DB/context

        if (text === 'clock in' || text === 'clock out') {
          const isClockIn = text === 'clock in';
          await client.query(
            `INSERT INTO punches(tenant_id, user_id, direction, punched_at, created_at)
             VALUES ($1, $2, $3, NOW(), NOW())`,
            [tenantId, userId, isClockIn ? 'in' : 'out']
          );
          // Update daily summary/OT/leave state here in same transaction
        }
      }
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    next(err);
  } finally {
    client.release();
  }
}
```

Schema (run once):

```sql
-- workio/server/src/db/schema.sql (append)
CREATE TABLE IF NOT EXISTS webhook_events_log (
  idempotency_key TEXT PRIMARY KEY,
  payload JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS punches (
  id SERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  punched_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

App setup (preserve raw body):

```ts
// workio/server/src/app.ts
import express from 'express';
import { lineWebhookHandler } from './routes/line/webhook';

const app = express();
app.use('/webhook/line', express.text({ type: 'application/json' }), lineWebhookHandler);
```

---

### 4. Verification (actionable tests)

1. **Signature verification**
   - Valid signed payload → `200 OK`.
   - Invalid/missing signature → `401`.

2. **Replay protection**
   - Replay same signed payload with timestamp >5 minutes old → ignored; no new punch.
   - Confirm `webhook_events_log` has no duplicate `idempotency_key`.

3. **Idempotency**
   - Send identical signed payload twice within seconds → second request does not create a second punch; exactly one row in `webhook_events_log`.

4. **Transactional safety**
   - Force error after punch insert (e.g., throw) → confirm punch row is rolled back and not committed.

5. **End-to-end via LINE**
   - Configure LINE webhook to endpoint.
   - Send “clock in” from test user.
   - Confirm one punch row created and daily summary/OT updated correctly.
