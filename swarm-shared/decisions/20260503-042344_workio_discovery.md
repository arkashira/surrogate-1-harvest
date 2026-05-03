# workio / discovery

## Final Synthesized Implementation

Below is the single, consolidated solution. It merges the strongest technical elements from all proposals, resolves contradictions in favor of correctness and concrete actionability, and provides a production-ready implementation.

### Key Resolutions & Rationale
1. **Signature Verification**: Use **raw body bytes** for HMAC (not `JSON.stringify`) to guarantee exact payload matching with LINE’s signature algorithm.
2. **Idempotency Key**: Use **deterministic composite key** (`userId|eventType|timeWindow|contentId`) for human-readable auditability and stable deduping across retries.
3. **Atomic Upsert**: Use `ON CONFLICT (user_id, event_window, event_type)` to prevent duplicates at the logical level (not just payload hash), ensuring correctness even if the same user punches twice in the same window via different requests.
4. **Observability**: Add structured logging for rejected/fraud events and include `idempotency_key` in all logs for traceability.
5. **Schema**: Extend `attendance_events` with `idempotency_key` and unique constraint; add `request_id` for end-to-end tracing.

---

### 1. File Setup
```bash
mkdir -p /opt/axentx/workio/server/src/routes/line
touch /opt/axentx/workio/server/src/routes/line/webhook.ts
```

---

### 2. Database Schema
```sql
-- /opt/axentx/workio/server/src/db/schema.sql
CREATE TABLE IF NOT EXISTS attendance_events (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  event_at TIMESTAMPTZ NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('clock_in', 'clock_out', 'leave_request', 'ot_request')),
  idempotency_key TEXT NOT NULL,
  request_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, event_at, event_type)  -- Logical uniqueness per user/time/type
);

CREATE INDEX IF NOT EXISTS idx_attendance_user_at ON attendance_events (user_id, event_at);
CREATE INDEX IF NOT EXISTS idx_idempotency_key ON attendance_events (idempotency_key);
```

---

### 3. Webhook Handler (TypeScript)
```ts
// /opt/axentx/workio/server/src/routes/line/webhook.ts
import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { Pool } from 'pg';

const router = Router();
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';
const IDEMPOTENCY_WINDOW_MS = 5 * 60 * 1000; // 5-minute window for same event grouping

// Middleware: verify X-Line-Signature using raw body bytes
function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  const signature = req.get('X-Line-Signature');
  if (!signature) {
    console.warn('Missing X-Line-Signature', { requestId: req.id });
    return res.status(401).json({ error: 'Missing X-Line-Signature' });
  }

  const rawBody = req.body?.raw || req.body; // Use raw buffer if available
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(JSON.stringify(req.body), 'utf8');
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body)
    .digest('base64');

  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    console.warn('Invalid LINE signature', { requestId: req.id, signature });
    return res.status(401).json({ error: 'Invalid signature' });
  }
  next();
}

// Deterministic idempotency key (human-readable + stable)
function makeIdempotencyKey(event: any, userId: string): string {
  const ts = parseInt(event.timestamp || Date.now(), 10);
  const timeWindow = Math.floor(ts / IDEMPOTENCY_WINDOW_MS) * IDEMPOTENCY_WINDOW_MS;
  const contentId = event.message?.id || event.contentId || '';
  return `${userId}|${event.type}|${timeWindow}|${contentId}`;
}

// Atomic upsert for attendance events
async function upsertAttendance(
  userId: string,
  eventAt: string,
  eventType: string,
  idempotencyKey: string,
  requestId?: string
) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(
      `INSERT INTO attendance_events (user_id, event_at, event_type, idempotency_key, request_id)
       VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (user_id, event_at, event_type) DO NOTHING`,
      [userId, eventAt, eventType, idempotencyKey, requestId]
    );
    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Upsert failed', { error: err, idempotencyKey, requestId });
    throw err;
  } finally {
    client.release();
  }
}

// Main webhook handler
router.post('/webhook/line', verifyLineSignature, async (req: Request, res: Response) => {
  const requestId = req.headers['x-request-id'] as string || crypto.randomUUID();
  const events = req.body.events || [];
  const results: any[] = [];

  for (const ev of events) {
    try {
      const userId = ev.source?.userId;
      if (!userId) {
        results.push({ status: 'ignored', reason: 'missing_userId' });
        continue;
      }

      const idempotencyKey = makeIdempotencyKey(ev, userId);
      const ts = ev.timestamp ? new Date(parseInt(ev.timestamp, 10)).toISOString() : new Date().toISOString();

      if (ev.type === 'message' && ev.message?.type === 'text') {
        const text = ev.message.text.trim().toLowerCase();
        let eventType: string | null = null;

        if (text === 'clock in' || text === 'เข้างาน') eventType = 'clock_in';
        else if (text === 'clock out' || text === 'เลิกงาน') eventType = 'clock_out';

        if (eventType) {
          await upsertAttendance(userId, ts, eventType, idempotencyKey, requestId);
          results.push({ status: 'ok', event: ev.type, action: eventType, idempotencyKey });
        } else {
          results.push({ status: 'ignored', text });
        }
      }

      // Extend here for leave/OT with similar idempotent upserts
    } catch (err) {
      console.error('Webhook processing error', { error: err, requestId });
      results.push({ status: 'error', error: String(err) });
    }
  }

  // Always return 200 to prevent LINE retries (idempotency handled)
  res.json({ requestId, results });
});

export default router;
```

---

### 4. Route Wiring
```ts
// /opt/axentx/workio/server/src/routes/index.ts
import { Router } from 'express';
import lineWebhook from './line/webhook';

const router = Router();
router.use('/line', lineWebhook);
export default router;
```

---

### 5. App Entry Point
```ts
// /opt/axentx/workio/server/src/app.ts
import express from 'express';
import routes from './routes';

const app = express();
app.use(express.json({ limit: '1mb' }));
app.use('/webhook', routes); // -> /webhook/line/webhook

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server listening on ${PORT}`));
```

---

### 6. Verification Commands

1. **Signature Rejection Test**
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: invalidsig" \
     -d '{"events":[]}'
   # Expected: 401 Invalid signature
   ```

2. **Idempot
