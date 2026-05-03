# workio / discovery

## Final Synthesized Solution

Below is the single, production-ready change set that merges the strongest, non-contradictory insights from both proposals and resolves all conflicts in favor of **correctness + concrete actionability**.

---

## 1. Diagnosis (merged)

- **Missing end-to-end idempotency**: LINE webhooks, client taps, and backend retries can all introduce duplicates.
- **No signature time-window enforcement**: Accepting LINE signatures without a short TTL enables replay attacks and stale event processing.
- **No transactional upsert on clock events**: concurrent retries can race and insert multiple rows for the same user/day instead of one canonical event.
- **No client-side request deduplication or pending state**: double-taps create duplicate intents and visual jank while waiting for round-trip.
- **Missing propagation of a stable deduplication key**: backend cannot reliably collapse duplicates without a client-provided or webhook-derived idempotency key.

---

## 2. Scope & Deliverables

- **Backend**
  - `server/src/db/migrations/0003_idempotency_and_events.sql`
  - `server/src/middleware/idempotency.ts`
  - `server/src/routes/webhook/line.ts`
  - `server/src/services/clock.ts` (transactional upsert)
- **Frontend**
  - `workio/src/hooks/useClockInOut.ts`
  - Component usage with stable dedupe key + optimistic UI

---

## 3. Implementation

### 3.1 DB migration (idempotency + transactional safety)

```sql
-- server/src/db/migrations/0003_idempotency_and_events.sql
CREATE TABLE IF NOT EXISTS processed_events (
  id BIGSERIAL PRIMARY KEY,
  event_id VARCHAR(255) NOT NULL,
  event_type VARCHAR(100) NOT NULL,
  source VARCHAR(50) NOT NULL DEFAULT 'line',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash VARCHAR(64) NULL,
  UNIQUE (event_id, source)
);

-- Clock events keyed by user+date+event_type to enforce one canonical event per direction per day
-- with idempotent upserts via dedupe_key
CREATE TABLE IF NOT EXISTS clock_events (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  event_type VARCHAR(10) NOT NULL CHECK (event_type IN ('in', 'out')),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  dedupe_key VARCHAR(255) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, event_type, DATE(occurred_at)),
  UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_processed_events_event_id ON processed_events (event_id);
CREATE INDEX IF NOT EXISTS idx_clock_events_user_date ON clock_events (user_id, DATE(occurred_at));
CREATE INDEX IF NOT EXISTS idx_clock_events_dedupe ON clock_events (dedupe_key);
```

---

### 3.2 Idempotency middleware (signature window + dedupe)

```ts
// server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';
import { pool } from '../db';
import crypto from 'crypto';

const SIGNATURE_TTL_MS = 5 * 60 * 1000; // 5 minutes (strict, aligns with security best practice)
const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

function verifyHmac(rawBody: string, signature: string): boolean {
  if (!LINE_CHANNEL_SECRET) return true; // skip only if misconfigured
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

export async function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  const signature = req.headers['x-line-signature'] as string;
  const timestamp = req.headers['x-line-timestamp'] as string;
  const rawBody = (req as any).rawBody || JSON.stringify(req.body);

  if (!signature || !timestamp) {
    return res.status(400).json({ error: 'Missing LINE signature headers' });
  }

  const ts = Number(timestamp);
  if (Number.isNaN(ts) || Math.abs(Date.now() - ts) > SIGNATURE_TTL_MS) {
    return res.status(400).json({ error: 'Signature expired or invalid timestamp' });
  }

  if (!verifyHmac(rawBody, signature)) {
    return res.status(400).json({ error: 'Invalid signature' });
  }

  (req as any).lineVerified = true;
  next();
}

export async function dedupeLineEvent(req: Request, res: Response, next: NextFunction) {
  const events = req.body.events || [];
  if (!events.length) return next();

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const filtered = [];

    for (const ev of events) {
      const eventId = ev.webhookEventId || ev.id;
      if (!eventId) continue;

      const { rows } = await client.query(
        `SELECT 1 FROM processed_events WHERE event_id = $1 AND source = 'line'`,
        [eventId]
      );

      if (rows.length) {
        continue; // duplicate — skip processing but acknowledge
      }

      await client.query(
        `INSERT INTO processed_events (event_id, event_type, source, payload_hash)
         VALUES ($1, $2, $3, $4)`,
        [eventId, ev.type || 'unknown', 'line', '']
      );

      filtered.push(ev);
    }

    await client.query('COMMIT');
    req.body.events = filtered;
    next();
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Idempotency error:', err);
    return res.status(500).json({ error: 'Idempotency check failed' });
  } finally {
    client.release();
  }
}
```

---

### 3.3 Webhook route (strict verification + dedupe)

```ts
// server/src/routes/webhook/line.ts
import express from 'express';
import { verifyLineSignature, dedupeLineEvent } from '../../middleware/idempotency';
import { handleLineEvent } from '../../services/line';

const router = express.Router();

// Preserve raw body for HMAC verification
function rawBodySaver(req: express.Request, _res: express.Response, buf: Buffer) {
  (req as any).rawBody = buf.toString('utf8');
}

router.post(
  '/line',
  express.json({ limit: '1mb', verify: rawBodySaver }),
  verifyLineSignature,
  dedupeLineEvent,
  async (req, res) => {
    try {
      const events = req.body.events || [];
      for (const ev of events) {
        await handleLineEvent(ev).catch((err) => {
          console.error('Event handling failed:', ev.type, err);
        });
      }
      res.status(200).json({ ok: true });
    } catch (err) {
      res.status(500).json({ error: 'Processing failed' });
    }
  }
);

export default router;
```

---

### 3.4 Transactional clock handler (backend upsert)

```ts
// server/src/services/clock.ts
import { pool } from '../db';

export async function recordClockEvent({
  userId,
  type,
  occurredAt = new Date(),
  dedupeKey,
}: {
  userId: number;
  type: 'in' | 'out';
  occurredAt?: Date;
  dedupeKey: string;
}) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Try insert first (fast path)
    try {
      await client.query(
        `INSERT INTO clock_events (user_id, event_type, occurred_at, dedupe_key)
         VALUES ($1, $2, $3, $4)`,
        [userId, type, occurredAt, dedupeKey]
      );
      await client.query('COMMIT');
      return
