# workio / discovery

## 1. Diagnosis
- Missing `X-Line-Signature` verification on `/webhook/line` allows spoofed/replayed clock-in/out, leave, and OT events to be accepted as valid.
- No idempotency/replay protection — LINE retries on 5xx/timeouts create duplicate `attendance_punches`, `leave_requests`, `ot_requests`.
- No request deduplication key in DB layer; duplicate webhook deliveries produce double entries and incorrect balances.
- Webhook handler processes events synchronously without early validation, increasing surface for abuse and retry storms.
- No defense-in-depth logging of signature failures or replay attempts for audit/ops.

## 2. Proposed change
- **File:** `workio/server/src/routes/lineWebhook.ts` (or equivalent under `server/src/routes/` or `server/src/controllers/`)
- **Scope:** Add `verifyLineSignature()` middleware and idempotency guard keyed by `X-Line-Signature` + `events[].id` (or `timestamp+source.userId+type` fallback), plus DB unique constraint/index on `(event_id, tenant_id)` for dedupe.
- **Add:** small util `verifyLineSignature.ts` and migration for idempotency index.

## 3. Implementation

### 3.1 Create signature verification util
`workio/server/src/utils/verifyLineSignature.ts`
```ts
import crypto from 'crypto';

export function verifyLineSignature(
  rawBody: string,
  signature: string | undefined,
  channelSecret: string
): boolean {
  if (!signature) return false;
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody, 'utf8')
    .digest('base64');
  // LINE uses base64 signature; constant-time compare
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}
```

### 3.2 Add idempotency middleware + apply to route
`workio/server/src/middleware/lineIdempotency.ts`
```ts
import { Request, Response, NextFunction } from 'express';
import { Pool } from 'pg';
import { verifyLineSignature } from '../utils/verifyLineSignature.js';

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

export async function lineSignatureAndIdempotency(
  req: Request,
  res: Response,
  next: NextFunction
) {
  const signature = req.get('X-Line-Signature');
  const rawBody = req.rawBody as string | undefined;
  const channelSecret = process.env.LINE_CHANNEL_SECRET || '';

  // 1) Signature verification
  if (!rawBody || !verifyLineSignature(rawBody, signature, channelSecret)) {
    console.warn('[line-webhook] Invalid signature', {
      hasRawBody: !!rawBody,
      hasSignature: !!signature,
    });
    return res.status(401).json({ error: 'Invalid signature' });
  }

  // 2) Parse events and collect event IDs for idempotency check
  let events;
  try {
    events = req.body?.events;
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(400).json({ error: 'No events' });
    }
  } catch {
    return res.status(400).json({ error: 'Invalid payload' });
  }

  const client = await pool.connect();
  try {
    // Build placeholders and values for multi-row upsert check
    const placeholders: string[] = [];
    const values: (string | null)[] = [];
    events.forEach((ev: any, idx: number) => {
      const eventId = ev?.id || `${ev?.timestamp || Date.now()}-${ev?.source?.userId || 'unknown'}-${ev?.type || 'unknown'}`;
      placeholders.push(`($${idx * 2 + 1}, $${idx * 2 + 2})`);
      values.push(eventId, req.tenantId || null);
    });

    // Check existing event_ids in one roundtrip
    const checkQuery = `
      SELECT event_id FROM line_event_idempotency
      WHERE (event_id, tenant_id) IN (${placeholders.map((_, i) => `($${i * 2 + 1}, $${i * 2 + 2})`).join(', ')})
    `;
    const checkRes = await client.query(checkQuery, values);
    const existing = new Set(checkRes.rows.map((r) => r.event_id));

    // If all events already processed, short-circuit with 200 (idempotent success)
    const allProcessed = events.every((ev: any) => {
      const eventId = ev?.id || `${ev?.timestamp || Date.now()}-${ev?.source?.userId || 'unknown'}-${ev?.type || 'unknown'}`;
      return existing.has(eventId);
    });
    if (allProcessed) {
      return res.status(200).json({ ok: true, note: 'already processed' });
    }

    // Attach client and metadata for downstream handler to commit processed IDs after successful business logic
    res.locals.lineIdempotency = { client, eventsToRecord: events, existing };
    next();
  } catch (err) {
    client.release();
    console.error('[line-webhook] idempotency check failed', err);
    return res.status(500).json({ error: 'Idempotency check failed' });
  }
}

export async function recordProcessedEvents(req: Request, res: Response, next: NextFunction) {
  const ctx = res.locals.lineIdempotency;
  if (!ctx) return next();

  const { client, eventsToRecord, existing } = ctx;
  try {
    const toInsert = (eventsToRecord as any[]).filter((ev) => {
      const eventId = ev?.id || `${ev?.timestamp || Date.now()}-${ev?.source?.userId || 'unknown'}-${ev?.type || 'unknown'}`;
      return !existing.has(eventId);
    });

    if (toInsert.length === 0) {
      client.release();
      return next();
    }

    const values = toInsert.flatMap((ev, idx) => {
      const eventId = ev?.id || `${ev?.timestamp || Date.now()}-${ev?.source?.userId || 'unknown'}-${ev?.type || 'unknown'}`;
      return [eventId, req.tenantId || null];
    });

    const placeholders = toInsert.map((_, i) => `($${i * 2 + 1}, $${i * 2 + 2})`).join(', ');
    await client.query(
      `INSERT INTO line_event_idempotency (event_id, tenant_id) VALUES ${placeholders} ON CONFLICT DO NOTHING`,
      values
    );
    client.release();
    next();
  } catch (err) {
    try { client.release(); } catch {}
    console.error('[line-webhook] record processed events failed', err);
    // Non-fatal: continue so business logic isn't blocked by logging failure
    next();
  }
}
```

### 3.3 Migration for idempotency table
`workio/server/src/db/migrations/20260504000000_line_event_idempotency.sql`
```sql
CREATE TABLE IF NOT EXISTS line_event_idempotency (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL,
  tenant_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uniq_line_event_idempotency UNIQUE (event_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_line_event_idempotency_lookup
ON line_event_idempotency (event_id, tenant_id);
```

### 3.4 Update webhook route to use middleware
`workio/server/src/routes/lineWebhook.ts` (adjust path/name to match actual)
```ts
import express from 'express';
import { lineSignatureAndIdempotency, recordProcessedEvents } from '../middleware/lineIdempotency.js';
import { handleLineEvent } from '../controllers/lineController.js';

const router = express.Router();

// Middleware to capture raw body for signature verification
router.use((req, res, next) => {
  const chunks: Buffer[] = [];
  req.on('data', (chunk) => chunks.push(chunk));
  req.on('end', () => {
    req.rawBody = Buffer.concat(chunks).toString('utf8
