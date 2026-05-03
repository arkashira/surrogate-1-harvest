# workio / discovery

## Final Consolidated Implementation  
*(Best parts of Candidate 1 + Candidate 2; contradictions resolved for correctness + concrete actionability)*

### 1. Diagnosis (merged, prioritized)
- **Missing `X-Line-Signature` verification** — allows spoofed/replayed events to mutate attendance/leave/OT.
- **No idempotency/replay protection** — LINE retries on 5xx/timeouts create duplicate `attendance_punch` rows and duplicate leave/OT requests.
- **No request timestamp window check** — events can be replayed hours/days later.
- **No atomic punch upsert** — concurrent retries can create multiple punches for the same user within a short window.
- **No immutable audit trail** — no durable log of raw payload, signature, and outcome for forensics/debugging.

### 2. Schema changes (run once)

```sql
-- Idempotency + replay protection
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  idempotency_key VARCHAR(255) PRIMARY KEY,
  event_type      VARCHAR(100) NOT NULL,
  payload_hash    VARCHAR(64)  NOT NULL,
  processed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Optional: auto-expire keys after 48h
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_processed_at
  ON webhook_deliveries (processed_at);

-- Immutable audit log for forensics
CREATE TABLE IF NOT EXISTS webhook_audit_log (
  id              BIGSERIAL PRIMARY KEY,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  signature       TEXT        NOT NULL,
  raw_payload     TEXT        NOT NULL,
  event_type      VARCHAR(100),
  user_id         VARCHAR(255),
  idempotency_key VARCHAR(255),
  outcome         VARCHAR(50) NOT NULL, -- 'accepted', 'duplicate', 'rejected', 'error'
  error_detail    TEXT
);

-- Attendance punch with atomic upsert support
-- Assumes one punch per user per source per short time window (e.g., per minute)
CREATE TABLE IF NOT EXISTS attendance_punch (
  id              BIGSERIAL PRIMARY KEY,
  user_id         VARCHAR(255) NOT NULL,
  punch_time      TIMESTAMPTZ  NOT NULL,
  source          VARCHAR(50)  NOT NULL, -- e.g., 'line'
  event_id        VARCHAR(255),           -- LINE event idempotency key
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, source, punch_time) -- or use a deterministic time-bucket expression if needed
);
```

### 3. Core library modules

```ts
// server/src/lib/lineSecurity.ts
import crypto from 'crypto';

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

export function verifyLineSignature(rawBody: string, signature: string): boolean {
  if (!LINE_CHANNEL_SECRET) {
    // Dev convenience only; never disable in prod
    console.warn('LINE_CHANNEL_SECRET missing; skipping signature verification');
    return true;
  }
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

export function isTimestampFresh(timestamp: number, windowMs = 5 * 60 * 1000): boolean {
  return Math.abs(Date.now() - timestamp) <= windowMs;
}
```

```ts
// server/src/lib/idempotency.ts
import { Pool } from 'pg';
import crypto from 'crypto';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

export async function isDuplicate(key: string, payload: any): Promise<boolean> {
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex');
  const client = await pool.connect();
  try {
    const { rows } = await client.query(
      'SELECT 1 FROM webhook_deliveries WHERE idempotency_key = $1 AND payload_hash = $2',
      [key, payloadHash]
    );
    return rows.length > 0;
  } finally {
    client.release();
  }
}

export async function recordDelivery(key: string, payload: any, eventType: string): Promise<void> {
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex');
  const client = await pool.connect();
  try {
    await client.query(
      'INSERT INTO webhook_deliveries(idempotency_key, event_type, payload_hash) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
      [key, eventType, payloadHash]
    );
  } finally {
    client.release();
  }
}

export async function auditLog(
  signature: string,
  rawPayload: string,
  eventType: string | null,
  userId: string | null,
  idempotencyKey: string | null,
  outcome: 'accepted' | 'duplicate' | 'rejected' | 'error',
  errorDetail?: string
): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query(
      'INSERT INTO webhook_audit_log(signature, raw_payload, event_type, user_id, idempotency_key, outcome, error_detail) VALUES ($1, $2, $3, $4, $5, $6, $7)',
      [signature, rawPayload, eventType, userId, idempotencyKey, outcome, errorDetail || null]
    );
  } finally {
    client.release();
  }
}
```

```ts
// server/src/lib/attendance.ts
import { Pool } from 'pg';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

export async function upsertPunch(
  userId: string,
  punchTime: Date,
  source: string,
  eventId: string
): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    // Atomic upsert: if conflict on (user_id, source, punch_time), do nothing (idempotent)
    await client.query(
      'INSERT INTO attendance_punch(user_id, punch_time, source, event_id) VALUES ($1, $2, $3, $4) ON CONFLICT (user_id, source, punch_time) DO NOTHING',
      [userId, punchTime, source, eventId]
    );
    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

### 4. Webhook route (single source of truth)

```ts
// server/src/routes/webhook/line.ts
import express from 'express';
import { verifyLineSignature, isTimestampFresh } from '../../lib/lineSecurity';
import { isDuplicate, recordDelivery, auditLog } from '../../lib/idempotency';
import { upsertPunch } from '../../lib/attendance';

const router = express.Router();

router.post('/webhook/line', express.raw({ type: 'application/json' }), async (req, res) => {
  const signature = (req.headers['x-line-signature'] as string) || '';
  const rawBody = req.body.toString('utf8');

  // 1) Signature verification
  if (!verifyLineSignature(rawBody, signature)) {
    await auditLog(signature, rawBody, null, null, null, 'rejected', 'Invalid signature');
    return res.status(401).send('Invalid signature');
  }

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    await auditLog(signature, rawBody, null, null, null, 'rejected', 'Invalid JSON');
    return res.status(400).send('Invalid JSON');
  }

  const events = payload.events || [];
  if (events.length === 0) {
    await auditLog(signature, rawBody, null, null, null, 'accepted', undefined);
    return res.status(200).send('OK');
  }

  // Process events sequentially; each event gets its own idempotency key and audit record
  for (const ev of events) {
    const userId = ev.source?.userId || null;
