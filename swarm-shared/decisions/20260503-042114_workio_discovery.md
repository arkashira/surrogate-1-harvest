# workio / discovery

## Final Synthesized Solution

### Diagnosis (Consolidated)
- **Missing idempotency**: LINE retries and replays create duplicate attendance/leave/OT records.
- **Missing signature verification**: `X-Line-Signature` is not validated, enabling spoofed events and attendance fraud.
- **Race conditions on intent**: no per-tenant/per-day/per-employee guard allows multiple active sessions or duplicate leave/OT requests.
- **Non-atomic event writes**: concurrent requests can insert duplicates instead of “latest wins” or “first-wins.”
- **No stored deduplication key**: impossible to detect or suppress duplicates after ingestion.

---

### Proposed Change (Single Source of Truth)
- **File**: `workio/server/src/routes/webhook/line.ts` (create or update).
- **Scope**:
  1. Verify `X-Line-Signature` using raw-body capture and HMAC-SHA256.
  2. Compute and enforce an idempotency key (`event.id` + `tenantId`) stored in `webhook_events` with a unique constraint.
  3. Use atomic upsert for `attendance_events` keyed by `(tenant_id, employee_id, date, intent)` to enforce “latest wins” for clock-in/out.
  4. Enforce intent guard for leave/OT: at most one pending request per employee per day per type (use DB unique constraint + status).
  5. Return `200 OK` immediately after dedupe/validation to stop LINE retries; process asynchronously where safe.

---

### Implementation

#### 1. Database migrations
```sql
-- server/src/db/migrations/001_webhook_idempotency.sql
CREATE TABLE IF NOT EXISTS webhook_events (
  idempotency_key TEXT NOT NULL,
  event_id        TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (idempotency_key)
);

-- Ensure deterministic key for attendance upsert (adjust column names to your schema)
-- Example:
-- ALTER TABLE attendance_events
--   ADD CONSTRAINT uq_tenant_employee_date_intent
--   UNIQUE (tenant_id, employee_id, date, intent);

-- For leave/OT intent guard (pending state)
-- ALTER TABLE leave_requests
--   ADD CONSTRAINT uq_tenant_employee_date_type_pending
--   UNIQUE (tenant_id, employee_id, date, type)
--   WHERE status = 'pending';
```

#### 2. Signature verification utility
```ts
// server/src/utils/line.ts
import crypto from 'crypto';

export function verifyLineSignature(body: string, signature: string, channelSecret: string): boolean {
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(body)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}
```

#### 3. Webhook route
```ts
// server/src/routes/webhook/line.ts
import crypto from 'crypto';
import express from 'express';
import db from '../../db';
import { verifyLineSignature } from '../../utils/line';

const router = express.Router();

function buildIdempotencyKey(event: any, tenantId: string): string {
  return crypto
    .createHash('sha256')
    .update(`${event.id}:${tenantId}`)
    .digest('hex');
}

async function handleClockEvent(
  tenantId: string,
  employeeId: string,
  intent: 'clock_in' | 'clock_out',
  occurredAt: Date,
  location?: { lat: number; lng: number }
) {
  const date = occurredAt.toISOString().split('T')[0];
  await db.query(
    `INSERT INTO attendance_events (tenant_id, employee_id, date, intent, occurred_at, location, created_at, updated_at)
     VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
     ON CONFLICT (tenant_id, employee_id, date, intent)
     DO UPDATE SET
       occurred_at = EXCLUDED.occurred_at,
       location = EXCLUDED.location,
       updated_at = NOW()`,
    [tenantId, employeeId, date, intent, occurredAt, location ? JSON.stringify(location) : null]
  );
}

async function createPendingLeaveOrOT(
  tenantId: string,
  employeeId: string,
  type: 'leave' | 'ot',
  date: string,
  details: any
) {
  // Intent guard: one pending per employee per day per type
  await db.query(
    `INSERT INTO ${type === 'leave' ? 'leave_requests' : 'ot_requests'}
       (tenant_id, employee_id, date, type, status, details, created_at, updated_at)
     VALUES ($1, $2, $3, $4, 'pending', $5, NOW(), NOW())
     ON CONFLICT (tenant_id, employee_id, date, type)
     DO UPDATE SET
       status = EXCLUDED.status,
       details = EXCLUDED.details,
       updated_at = NOW()
     WHERE ${type === 'leave' ? 'leave_requests' : 'ot_requests'}.status = 'pending'`,
    [tenantId, employeeId, date, type, JSON.stringify(details)]
  );
}

router.post('/line', async (req, res) => {
  const signature = req.get('X-Line-Signature');
  const rawBody = (req as any).rawBody || JSON.stringify(req.body);
  const tenantId = req.headers['x-tenant-id'] as string;

  if (!signature || !verifyLineSignature(rawBody, signature, process.env.LINE_CHANNEL_SECRET!)) {
    return res.status(401).send('Invalid signature');
  }

  const events = req.body.events || [];
  const results = { processed: 0, duplicates: 0, errors: 0 };

  for (const event of events) {
    try {
      const idemKey = buildIdempotencyKey(event, tenantId);

      const exists = await db.query(
        'SELECT 1 FROM webhook_events WHERE idempotency_key = $1',
        [idemKey]
      );
      if (exists.rows.length > 0) {
        results.duplicates++;
        continue;
      }

      await db.query(
        'INSERT INTO webhook_events (idempotency_key, event_id, tenant_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
        [idemKey, event.id, tenantId]
      );

      const employee = await db.query(
        'SELECT id FROM employees WHERE line_user_id = $1 AND tenant_id = $2',
        [event.source.userId, tenantId]
      );
      if (!employee.rows.length) continue;

      const employeeId = employee.rows[0].id;

      if (event.type === 'message' && event.message.type === 'text') {
        const text = event.message.text.trim().toLowerCase();
        const now = new Date(event.timestamp);
        const date = now.toISOString().split('T')[0];

        if (text === 'clock in' || text === 'เข้างาน') {
          await handleClockEvent(tenantId, employeeId, 'clock_in', now, event.source?.area);
          results.processed++;
        } else if (text === 'clock out' || text === 'เลิกงาน') {
          await handleClockEvent(tenantId, employeeId, 'clock_out', now, event.source?.area);
          results.processed++;
        } else if (text === 'request leave' || text === 'ลา') {
          await createPendingLeaveOrOT(tenantId, employeeId, 'leave', date, { requestedBy: employeeId, via: 'line' });
          results.processed++;
        } else if (text === 'request ot' || text === 'ขอ ot') {
          await createPendingLeaveOrOT(tenantId, employeeId, 'ot', date, { requestedBy: employeeId, via: 'line' });
          results.processed++;
        }
      }
    } catch (err) {
      results.errors++;
      console.error('LINE webhook processing error:', err);
    }
  }

  res.status(200).send('OK');
});

export default router;
```

#### 4. App setup (raw body capture)
```ts
// server/src/app.ts (or server/src/index.ts)
import express from 'express';
import lineRouter from './routes/webhook/line';

const app =
