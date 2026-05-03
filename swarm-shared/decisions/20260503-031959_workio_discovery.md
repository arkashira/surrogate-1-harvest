# workio / discovery

## 1. Diagnosis

- **Missing `X-Line-Signature` verification** on `/webhook/line` allows spoofed/replayed events to be accepted as valid clock-in/out, leave, or OT requests.
- **No idempotency/replay protection** — LINE retries on 5xx/timeouts create duplicate `attendance_punches`, `leave_requests`, `ot_requests`.
- **No request-level deduplication key** in DB layer (no unique constraint/index on `line_event_id` or similar) to prevent duplicates even if application logic races.
- **No constant-time signature comparison** — timing side-channels possible if naive string equality is used.
- **No early rejection for malformed/unsigned requests** — unverified payloads reach business logic and DB writes.

## 2. Proposed change

- **File**: `workio/server/src/routes/webhook/line.ts` (or equivalent route handling `/webhook/line`)
- **Scope**: Add middleware that:
  1. Reads `X-Line-Signature` header and raw body (Buffer).
  2. Verifies HMAC-SHA256 signature using `channelSecret`.
  3. Rejects with `401` on mismatch or missing header.
  4. Adds idempotency by storing `line_event_id` (from each event object) with a unique constraint and skipping already-processed events.
- **DB**: Add unique index on `attendance_punches(line_event_id)` (nullable), same for `leave_requests` and `ot_requests` if they store per-event ids; or add a single `processed_webhook_events` table keyed by `line_event_id` for cross-entity dedupe.

## 3. Implementation

```bash
# Ensure route file exists; create if needed
mkdir -p /opt/axentx/workio/server/src/routes/webhook
```

```ts
// /opt/axentx/workio/server/src/routes/webhook/line.ts
import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { pool } from '../../db';

const router = Router();

const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';
if (!CHANNEL_SECRET) {
  console.warn('LINE_CHANNEL_SECRET is not set');
}

function verifySignature(body: Buffer, signature: string): boolean {
  if (!signature || !CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(body)
    .digest('base64');
  // constant-time comparison
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

// Idempotency: ensure processed_event tracking table exists
async function ensureProcessedTable() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS processed_webhook_events (
      line_event_id TEXT PRIMARY KEY,
      processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
  `);
}
ensureProcessedTable().catch(console.error);

async function isAlreadyProcessed(lineEventId: string): Promise<boolean> {
  const res = await pool.query(
    'SELECT 1 FROM processed_webhook_events WHERE line_event_id = $1',
    [lineEventId]
  );
  return res.rowCount > 0;
}

async function markProcessed(lineEventId: string) {
  try {
    await pool.query(
      'INSERT INTO processed_webhook_events (line_event_id) VALUES ($1) ON CONFLICT DO NOTHING',
      [lineEventId]
    );
  } catch (err) {
    // ignore race duplicates
  }
}

// Middleware: verify signature and raw body
router.use((req: Request, res: Response, next: NextFunction) => {
  const signature = req.get('X-Line-Signature') || '';
  // body must be raw Buffer; ensure body-parser raw middleware runs before this for this route
  const raw = (req as any).rawBody as Buffer;
  if (!raw || !verifySignature(raw, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }
  next();
});

// Main webhook handler
router.post('/line', async (req: Request, res: Response) => {
  try {
    const events = req.body.events || [];
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(200).json({ ok: true });
    }

    for (const ev of events) {
      const lineEventId = ev.source?.userId && ev.timestamp
        ? `${ev.source.userId}:${ev.type}:${ev.timestamp}`
        : ev.webhookEventId || null;

      // Prefer platform event id if present
      const platformId = ev.webhookEventId || lineEventId;
      if (!platformId) continue;

      if (await isAlreadyProcessed(platformId)) {
        continue; // skip duplicate
      }

      // Process event types (simplified)
      if (ev.type === 'message' && ev.message?.type === 'text') {
        // Example: parse clock-in command
        const text = ev.message.text.trim();
        const userId = ev.source.userId;
        if (text === 'clock in' || text === 'clock out') {
          await pool.query(
            'INSERT INTO attendance_punches (user_id, punch_type, line_event_id, created_at) VALUES ($1, $2, $3, NOW()) ON CONFLICT DO NOTHING',
            [userId, text === 'clock in' ? 'in' : 'out', platformId]
          );
        }
      } else if (ev.type === 'postback') {
        // handle leave/OT postbacks similarly
        // mark processed only after successful handling
      }

      await markProcessed(platformId);
    }

    res.status(200).json({ ok: true });
  } catch (err) {
    console.error('Webhook processing error:', err);
    // LINE will retry on 5xx — idempotency prevents duplicates
    res.status(500).json({ error: 'Processing failed' });
  }
});

export default router;
```

```ts
// /opt/axentx/workio/server/src/app.ts (or main server file) — mount raw-body middleware for this route
import express from 'express';
import lineWebhookRouter from './routes/webhook/line';

const app = express();

// Body parser for JSON (other routes)
app.use(express.json());

// For LINE webhook route only: preserve raw body
app.use(
  '/webhook/line',
  (req, res, next) => {
    const chunks: Buffer[] = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      (req as any).rawBody = Buffer.concat(chunks);
      next();
    });
  }
);

app.use('/webhook/line', lineWebhookRouter);

export default app;
```

```sql
-- /opt/axentx/workio/server/src/db/schema.sql additions
-- Ensure idempotency columns/indexes exist
ALTER TABLE attendance_punches ADD COLUMN IF NOT EXISTS line_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_punches_line_event_id
  ON attendance_punches (line_event_id)
  WHERE line_event_id IS NOT NULL;

-- Repeat for other entities if needed
ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS line_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_leave_requests_line_event_id
  ON leave_requests (line_event_id)
  WHERE line_event_id IS NOT NULL;

ALTER TABLE ot_requests ADD COLUMN IF NOT EXISTS line_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ot_requests_line_event_id
  ON ot_requests (line_event_id)
  WHERE line_event_id IS NOT NULL;
```

## 4. Verification

1. **Signature verification**
   - Start server and send a POST to `/webhook/line` without `X-Line-Signature` → expect `401`.
   - Send with a wrong signature → expect `401`.
   - Compute valid signature locally:
     ```bash
     echo -n '{"events":[]}' | openssl dgst -sha256 -hmac "$LINE_CHANNEL_SECRET" -binary | base64
     ```
     Send with that header and body → expect `200`.

2. **Idempotency**
   - Send the same valid payload twice (same `webhookEventId` or synthesized id) → second request should not create a new `attendance_punches` row.
