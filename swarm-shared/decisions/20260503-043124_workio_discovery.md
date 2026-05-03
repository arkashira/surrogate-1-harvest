# workio / discovery

## 1. Diagnosis
- LINE webhook ingestion has **no `X-Line-Signature` verification** — accepts spoofed events enabling attendance fraud and phantom clock-ins/outs.
- **No idempotency guard** on webhook handler — LINE retries on 5xx/timeouts create duplicate clock/leave/OT records.
- Clock-in/out and leave/OT mutations use fire-and-forget POSTs with **no optimistic UI or local rollback** → slow perceived performance and jarring full-page reflows on error.
- **No local-first draft persistence** for partially-completed leave/OT/clock notes → data loss on navigation or accidental refresh.
- Backend `.env` secrets and LINE channel credentials are **not validated at startup** — silent misconfigurations surface only at runtime.

## 2. Proposed change
Secure and harden the LINE webhook endpoint (`/webhook/line`) with:
- `X-Line-Signature` HMAC-SHA256 verification
- Idempotency key dedupe (per `webhookEventId` or `deliveryId` + event type + userId + timestamp window)
- Atomic upsert for clock/leave/OT events to prevent duplicates
Scope: `workio/server/src/routes/lineWebhook.ts` (or equivalent) + small DB migration for idempotency table.

## 3. Implementation

### Add idempotency table (SQL)
```sql
-- server/src/db/migrations/001_idempotency.sql
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_type      TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash    TEXT NOT NULL,
  response_status INT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Keep keys for 24h (LINE retries window)
CREATE INDEX IF NOT EXISTS idx_line_idempotency_expiry
  ON line_webhook_idempotency (processed_at)
  WHERE processed_at > (NOW() - INTERVAL '24 hours');
```

### Harden webhook route
```ts
// server/src/routes/lineWebhook.ts
import crypto from 'crypto';
import express, { Request, Response } from 'express';
import { db } from '../db/index.js';
import { processClockEvent, processLeaveEvent, processOtEvent } from '../services/lineEvents.js';

const router = express.Router();
const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET;
if (!LINE_CHANNEL_SECRET) {
  throw new Error('LINE_CHANNEL_SECRET is required');
}

function verifySignature(body: Buffer, signature: string): boolean {
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function idempotencyKey(event: any): string {
  // Use deliveryId when available; fallback stable composite key
  const deliveryId = event.deliveryId || '';
  const type = event.type || '';
  const userId = (event.source && event.source.userId) || '';
  const ts = event.timestamp || Date.now();
  // Normalize to minute granularity for same-event replays within retry window
  const normalizedTs = new Date(ts).toISOString().slice(0, 16);
  return crypto
    .createHash('sha256')
    .update(`${deliveryId}:${type}:${userId}:${normalizedTs}`)
    .digest('hex');
}

async function handleEvent(event: any, tx: any) {
  switch (event.type) {
    case 'message':
      if (event.message.type === 'text') {
        // Example: text commands for leave/ot
        const text = event.message.text.trim().toLowerCase();
        if (text.startsWith('ลา')) return processLeaveEvent(event, tx);
        if (text.startsWith('โอที') || text.startsWith('ot')) return processOtEvent(event, tx);
      }
      break;
    case 'postback':
      // Clock in/out or leave/OT actions from templates
      const data = event.postback.data || '';
      if (data.startsWith('clock:')) return processClockEvent(event, tx);
      if (data.startsWith('leave:')) return processLeaveEvent(event, tx);
      if (data.startsWith('ot:')) return processOtEvent(event, tx);
      break;
    // Add other event types as needed
  }
  return { status: 200, ok: true };
}

router.post('/webhook/line', async (req: Request, res: Response) => {
  const signature = req.headers['x-line-signature'] as string;
  if (!signature) return res.status(401).json({ error: 'Missing X-Line-Signature' });

  const rawBody = Buffer.isBuffer(req.body) ? req.body : Buffer.from(JSON.stringify(req.body));
  if (!verifySignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const events = req.body.events || [];
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(200).json({ ok: true });
  }

  const client = await db.connect();
  try {
    await client.query('BEGIN');

    for (const event of events) {
      const key = idempotencyKey(event);
      const payloadHash = crypto.createHash('sha256').update(JSON.stringify(event)).digest('hex');

      // Check idempotency
      const exists = await client.query(
        'SELECT 1 FROM line_webhook_idempotency WHERE idempotency_key = $1',
        [key]
      );
      if (exists.rows.length > 0) {
        // Already processed — skip but continue processing other events
        continue;
      }

      const result = await handleEvent(event, client);
      await client.query(
        'INSERT INTO line_webhook_idempotency (idempotency_key, event_type, user_id, payload_hash, response_status) VALUES ($1, $2, $3, $4, $5)',
        [key, event.type || 'unknown', (event.source && event.source.userId) || 'unknown', payloadHash, result.status || 200]
      );
    }

    await client.query('COMMIT');
    return res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook processing failed:', err);
    return res.status(500).json({ error: 'Processing failed' });
  } finally {
    client.release();
  }
});

export default router;
```

### Add startup validation for LINE secrets
```ts
// server/src/index.ts (or app bootstrap)
if (!process.env.LINE_CHANNEL_SECRET || !process.env.LINE_CHANNEL_ACCESS_TOKEN) {
  console.error('FATAL: LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN must be set in .env');
  process.exit(1);
}
```

### Client-side optimistic UI + rollback (small, high-value)
```tsx
// workio/src/hooks/useOptimisticClock.ts
import { useState, useCallback } from 'react';

export function useOptimisticClock() {
  const [pending, setPending] = useState(false);
  const [lastAction, setLastAction] = useState<{ type: 'in' | 'out'; timestamp: number } | null>(null);

  const optimisticClock = useCallback(async (action: 'in' | 'out', fn: () => Promise<any>) => {
    setPending(true);
    const ts = Date.now();
    const prev = lastAction;
    setLastAction({ type: action, timestamp: ts });

    try {
      await fn();
    } catch (err) {
      // Rollback UI
      setLastAction(prev);
      // Optionally show toast
      console.error('Clock action failed:', err);
      throw err;
    } finally {
      setPending(false);
    }
  }, [lastAction]);

  return { pending, lastAction, optimisticClock };
}
```

Use in clock button:
```tsx
const { pending, optimisticClock } = useOptimisticClock();
const handleClockIn = () => optimisticClock('in', () => api.post('/clock/in', { location }));
```

## 4. Verification
- **Signature verification**: Send a test POST with a wrong/missing `X-Line
