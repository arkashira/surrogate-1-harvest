# workio / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

### 1. Diagnosis (merged, corrected)
- **No idempotency on LINE webhook ingestion**: duplicate `X-Line-Signature` replays (network retries, client double-taps) can double-apply clock-in/out and leave/OT transitions.
- **Signature verification not strict or not early**: must use constant-time HMAC-SHA256 check **before any side effects**; must preserve raw body for correct hash.
- **No replay-window or nonce tracking**: same `line_event_id` can be processed multiple times; need cheap, fast de-dup with short TTL.
- **Frontend optimistic UI without server idempotency**: client may optimistically apply state, but retries/replays diverge from source-of-truth.
- **Non-atomic upsert for time events per user per window**: concurrent/retried punches can create duplicate rows instead of idempotent update/ignore.

### 2. Proposed change (merged)
- **File scope**: `workio/server/src/routes/line/webhook.ts` (or equivalent) — enforce strict signature verification and idempotent processing before DB writes.
- **Add idempotency store**: small `line_webhook_idempotency` table (or Redis) keyed by `line_event_id` + `user_id` + `event_type`; reject duplicates within a 5-minute window.
- **Add frontend `useLinePunch` hook**: send `Idempotency-Key` (UUID) for manual punches; do **not** optimistically mutate UI; rely on server truth.
- **Manual punch endpoint**: mirror idempotency pattern (same or separate table) with deterministic response.

### 3. Implementation (merged + hardened)

#### 3.1 DB migration (idempotency table)
```sql
-- server/src/db/migrations/20260504_line_webhook_idempotency.sql
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  id              BIGSERIAL PRIMARY KEY,
  line_event_id   TEXT NOT NULL,
  user_id         BIGINT NOT NULL,
  event_type      TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Natural idempotency key
CREATE UNIQUE INDEX IF NOT EXISTS uq_line_event_user_type
ON line_webhook_idempotency (line_event_id, user_id, event_type);

-- Fast expiry (optional, keeps table small)
CREATE INDEX IF NOT EXISTS idx_line_webhook_idempotency_expiry
ON line_webhook_idempotency (created_at);
```

#### 3.2 Strict signature verification + idempotent handler
```ts
// server/src/routes/line/webhook.ts
import crypto from 'crypto';
import { db } from '../../db';
import { Request, Response } from 'express';

const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET!;

function verifySignature(body: string, signature: string): boolean {
  if (!signature) return false;
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(body, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

async function isDuplicateEvent(
  lineEventId: string,
  userId: number,
  eventType: string,
  payloadHash: string
): Promise<boolean> {
  // Try insert; if conflict and same hash => duplicate; if conflict and different hash => tamper (log + reject)
  const result = await db.query(
    `INSERT INTO line_webhook_idempotency (line_event_id, user_id, event_type, payload_hash)
     VALUES ($1, $2, $3, $4)
     ON CONFLICT (line_event_id, user_id, event_type) DO NOTHING
     RETURNING line_event_id`,
    [lineEventId, userId, eventType, payloadHash]
  );

  if (result.rowCount > 0) {
    // Fresh insert
    return false;
  }

  // Existed; check hash
  const existing = await db.query(
    `SELECT payload_hash FROM line_webhook_idempotency
     WHERE line_event_id = $1 AND user_id = $2 AND event_type = $3`,
    [lineEventId, userId, eventType]
  );

  if (existing.rowCount === 0) {
    // Race deletion edge-case: treat as non-duplicate to allow reprocess
    return false;
  }

  const same = existing.rows[0].payload_hash === payloadHash;
  return same; // duplicate if same hash; if different, caller should log/reject
}

export async function lineWebhookHandler(req: Request, res: Response) {
  const signature = req.get('X-Line-Signature');
  const rawBody = req.body; // raw buffer from express.raw()

  if (!signature || !verifySignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const events = req.body.events || [];
  const results = [];

  for (const ev of events) {
    const { type, source } = ev;
    // Resolve user_id via your real user mapping; example below is illustrative
    const userId = source?.userId ? parseInt(source.userId.replace(/[^\d]/g, ''), 10) : null;
    if (!userId) {
      results.push({ id: ev.id, status: 'skipped', reason: 'no_user' });
      continue;
    }

    const payloadHash = crypto
      .createHash('sha256')
      .update(JSON.stringify(ev))
      .digest('hex');

    const duplicate = await isDuplicateEvent(ev.id, userId, type, payloadHash);
    if (duplicate) {
      results.push({ id: ev.id, status: 'duplicate' });
      continue;
    }

    // If hash differs on conflict, log potential tamper and reject this event
    // (Handled above: if conflict but not same hash, isDuplicateEvent returns false but we can detect via second lookup)
    const existing = await db.query(
      `SELECT payload_hash FROM line_webhook_idempotency
       WHERE line_event_id = $1 AND user_id = $2 AND event_type = $3`,
      [ev.id, userId, type]
    );
    if (existing.rowCount > 0 && existing.rows[0].payload_hash !== payloadHash) {
      console.warn('LINE event tamper detected', { id: ev.id, userId });
      results.push({ id: ev.id, status: 'rejected', reason: 'tamper' });
      continue;
    }

    // Atomic upsert for punch state per user per day (example)
    try {
      await db.query(
        `INSERT INTO punches (user_id, event_type, line_event_id, payload, created_at)
         VALUES ($1, $2, $3, $4, NOW())
         ON CONFLICT (user_id, date_trunc('day', created_at))
         DO UPDATE
           SET event_type = EXCLUDED.event_type,
               line_event_id = EXCLUDED.line_event_id,
               payload = EXCLUDED.payload,
               updated_at = NOW()`,
        [userId, type, ev.id, JSON.stringify(ev)]
      );
      results.push({ id: ev.id, status: 'processed' });
    } catch (err) {
      console.error('Failed to process LINE event', err);
      results.push({ id: ev.id, status: 'error' });
    }
  }

  return res.json({ results });
}
```

**Mount with raw body** (critical for correct signature):
```ts
// server/src/routes/index.ts (or app setup)
import express from 'express';
import { lineWebhookHandler } from './line/webhook';

const app = express();
app.post(
  '/webhook/line',
  express.raw({ type: 'application/json' }),
  (req, res) => {
    try {
      req.body = JSON.parse(req.body.toString());
    } catch {
      req.body = { events: [] };
    }
    return lineWebhookHandler(req, res);
  }
);
```

#### 3.3 Frontend idempotency for manual punches
```tsx
// workio/src/hooks/useLinePunch.ts
import { useCallback } from 'react';
import { v4 as uuidv4 } from 'uuid';

export function useLinePunch() {
  const punch = useCallback(async (type: 'clock_in' | 'clock_out
