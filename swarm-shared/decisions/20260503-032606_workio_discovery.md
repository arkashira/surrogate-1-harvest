# workio / discovery

## Final consolidated implementation

**Core changes (minimal, <2 h scope)**

1. **Verify `X-Line-Signature`**  
   - Use raw-body middleware for `/webhook/line` so HMAC-SHA256 is computed on the exact bytes LINE signed.  
   - Reject missing/invalid signatures with `401`.

2. **Idempotency / replay protection**  
   - Deduplicate by `event.webhookEventId` (preferred) or `deliveryContext.isRedelivery`.  
   - Persist processed IDs in Postgres (single SQL migration) so multi-instance deployments are safe.  
   - Enforce DB-level uniqueness (`tenant_id`, `source_type`, `source_id`, `event_webhook_id`) on attendance/leave/OT rows to guarantee exactly-once side effects.

3. **Strict schema validation**  
   - Validate inbound payloads with Zod; reject invalid shapes with `400`.  
   - Coerce and validate command text (clock-in/out, leave types) with an allowlist.

4. **Explicit tenant scoping**  
   - Resolve `tenant_id` from `line_user_id` on every event before any side effect.  
   - Include `tenant_id` in all queries and use row-level security or WHERE clauses to prevent cross-tenant writes.

5. **Observability & ops**  
   - Structured logs (JSON) with request ID, event ID, tenant, outcome, latency.  
   - Metrics: webhook_received, webhook_verified, webhook_processed, webhook_rejected, duplicate_skipped.  
   - Return `200 OK` quickly after validation/queueing; process business logic asynchronously if possible.

---

### Install / setup

```bash
cd /opt/axentx/workio/server
npm install zod
# Ensure pg (or your db driver) is available
```

**Migration (PostgreSQL) — idempotency + uniqueness**

```sql
-- Track processed LINE events to guarantee idempotency across instances
CREATE TABLE IF NOT EXISTS line_processed_events (
  tenant_id      TEXT NOT NULL,
  event_webhook_id TEXT NOT NULL,
  processed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, event_webhook_id)
);

-- Prevent duplicate attendance punches per tenant+user+date+event
ALTER TABLE attendance_punch
  ADD COLUMN IF NOT EXISTS created_from_event_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_punch_tenant_user_date_event
  ON attendance_punch (tenant_id, user_id, DATE(punched_at), created_from_event_id)
  WHERE created_from_event_id IS NOT NULL;

-- Prevent duplicate leave/OT requests from same event
ALTER TABLE leave_requests
  ADD COLUMN IF NOT EXISTS created_from_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_leave_tenant_event
  ON leave_requests (tenant_id, created_from_event_id)
  WHERE created_from_event_id IS NOT NULL;

ALTER TABLE ot_requests
  ADD COLUMN IF NOT EXISTS created_from_event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ot_tenant_event
  ON ot_requests (tenant_id, created_from_event_id)
  WHERE created_from_event_id IS NOT NULL;
```

---

### Code: `/opt/axentx/workio/server/src/routes/webhook/line.ts`

```ts
import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import { z } from 'zod';
import { db } from '../db';
import { logger } from '../../utils/logger';
import { Counter, Histogram, Registry } from 'prom-client';

const router = Router();

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';
if (!LINE_CHANNEL_SECRET) {
  logger.warn('LINE_CHANNEL_SECRET is not configured');
}

// Metrics
const webhookReceived = new Counter({
  name: 'line_webhook_received_total',
  help: 'LINE webhook requests received',
  labelNames: ['tenant_id', 'outcome'],
  registers: [Registry.globalRegistry],
});
const webhookProcessed = new Counter({
  name: 'line_webhook_processed_total',
  help: 'LINE events processed',
  labelNames: ['tenant_id', 'type'],
  registers: [Registry.globalRegistry],
});
const webhookRejected = new Counter({
  name: 'line_webhook_rejected_total',
  help: 'LINE webhook rejected (bad sig/validation)',
  labelNames: ['reason'],
  registers: [Registry.globalRegistry],
});
const webhookDuration = new Histogram({
  name: 'line_webhook_duration_seconds',
  help: 'LINE webhook processing duration',
  buckets: [0.05, 0.1, 0.25, 0.5, 1, 2, 5],
  registers: [Registry.globalRegistry],
});

// Zod schemas
const LineWebhookBodySchema = z.object({
  destination: z.string(),
  events: z.array(
    z.object({
      type: z.string(),
      mode: z.string(),
      timestamp: z.number(),
      source: z.object({
        type: z.enum(['user', 'group', 'room']),
        userId: z.string(),
        groupId: z.string().optional(),
        roomId: z.string().optional(),
      }),
      webhookEventId: z.string(),
      deliveryContext: z.object({
        isRedelivery: z.boolean(),
      }),
      message: z
        .object({
          id: z.string(),
          type: z.string(),
          text: z.string().optional(),
        })
        .optional(),
    })
  ),
});

type LineWebhookBody = z.infer<typeof LineWebhookBodySchema>;

function verifySignature(rawBody: string | Buffer, signature: string): boolean {
  if (!LINE_CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

async function resolveTenantIdByUserId(userId: string): Promise<string | null> {
  const row = await db.oneOrNone<{ tenant_id: string }>(
    'SELECT tenant_id FROM users WHERE line_user_id = $1',
    [userId]
  );
  return row?.tenant_id || null;
}

async function isEventProcessed(tenantId: string, eventId: string): Promise<boolean> {
  const row = await db.oneOrNone<{ exists: boolean }>(
    'SELECT true FROM line_processed_events WHERE tenant_id = $1 AND event_webhook_id = $2',
    [tenantId, eventId]
  );
  return !!row;
}

async function markEventProcessed(tenantId: string, eventId: string): Promise<void> {
  try {
    await db.none(
      'INSERT INTO line_processed_events (tenant_id, event_webhook_id) VALUES ($1, $2) ON CONFLICT DO NOTHING',
      [tenantId, eventId]
    );
  } catch (err) {
    // If insert fails, still continue — uniqueness at DB level will protect side effects.
    logger.warn({ msg: 'Failed to persist processed event', tenantId, eventId, err });
  }
}

async function recordAttendance(tenantId: string, userId: string, punchedAt: number, eventId: string, command: 'in' | 'out') {
  const eventRef = `line:${userId}:${eventId}`;
  await db.none(
    `INSERT INTO attendance_punch (tenant_id, user_id, punched_at, created_from_event_id)
     VALUES ($1, $2, to_timestamp($3 / 1000.0), $4)
     ON CONFLICT (tenant_id, user_id, DATE(punched_at), created_from_event_id) DO NOTHING`,
    [tenantId, userId, punchedAt, eventRef]
  );
  logger.info({ msg: 'Attendance recorded', tenantId, userId, punchedAt, command, eventId });
}

async function requestLeave(tenantId: string, userId: string, leaveType: string, startDate: Date, endDate: Date, eventId: string) {
  const eventRef = `line:${userId}:${eventId}`;
  await db.none(
    `INSERT INTO leave_requests (tenant_id, user_id, leave_type, start_date, end_date, status, created_from_event_id)
     VALUES ($1, $2, $3, $4, $5, 'pending',
