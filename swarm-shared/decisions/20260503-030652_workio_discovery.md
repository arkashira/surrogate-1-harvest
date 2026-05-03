# workio / discovery

## 1. Diagnosis
- Missing **idempotency key enforcement** on LINE webhook handler allows duplicate clock-in/out events when LINE retries (network blips, slow 200 OK) → double punches, incorrect daily totals.
- No **replay protection / de-duplication store** (Redis or DB unique constraint) for `(tenant_id, employee_id, event_type, timestamp, line_delivery_id)`; race conditions under concurrency produce multiple rows.
- **`X-Line-Signature` verification is likely present but not enforced early**; unsigned or tampered payloads can reach business logic if middleware ordering is wrong.
- **No audit log for webhook ingestion** (request/response meta, timing, verification result) → hard to debug production issues and compliance gaps.
- **Schema allows logically duplicate punches** (missing unique constraint) → reporting queries must use fragile `DISTINCT`/windowing; data quality decays over time.

## 2. Proposed change
Add idempotent LINE webhook ingestion with signature verification, de-duplication, and audit logging:
- Files:  
  - `server/src/middleware/lineWebhook.ts` — verify signature + parse body early.  
  - `server/src/routes/webhook/line.ts` — idempotent handler, upsert punch, emit audit row.  
  - `server/src/db/schema.sql` — unique constraint/index on `(tenant_id, employee_id, event_type, event_at, line_delivery_id)` + `webhook_audit` table.

## 3. Implementation

### 3.1 DB schema additions (`server/src/db/schema.sql`)
```sql
-- Unique constraint to prevent logical duplicates
ALTER TABLE punches
  ADD CONSTRAINT uq_punch_line_delivery UNIQUE (tenant_id, employee_id, event_type, event_at, line_delivery_id);

-- Audit log for webhook ingestion
CREATE TABLE IF NOT EXISTS webhook_audit (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL,
  event_type      TEXT NOT NULL,
  line_delivery_id TEXT NOT NULL,
  employee_id     UUID,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified        BOOLEAN NOT NULL,
  source_ip       INET,
  user_agent      TEXT,
  payload_hash    TEXT,
  processing_ms   INTEGER,
  status_code     SMALLINT,
  error_message   TEXT
);

CREATE INDEX IF NOT EXISTS ix_webhook_audit_line_delivery ON webhook_audit(line_delivery_id);
CREATE INDEX IF NOT EXISTS ix_webhook_audit_received ON webhook_audit(received_at);
```

### 3.2 Middleware (`server/src/middleware/lineWebhook.ts`)
```ts
import crypto from 'crypto';
import { Request, Response, NextFunction } from 'express';

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

function verifyLineSignature(rawBody: Buffer, signature: string): boolean {
  if (!LINE_CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

export function lineWebhookMiddleware(req: Request, res: Response, next: NextFunction) {
  const signature = req.get('X-Line-Signature') || '';
  // Keep raw body for verification (use express.json({ verify: ... }) upstream)
  const rawBody = (req as any).rawBody as Buffer;
  if (!rawBody || !signature) {
    return res.status(400).json({ error: 'Missing body or signature' });
  }

  const verified = verifyLineSignature(rawBody, signature);
  (req as any).lineVerified = verified;
  (req as any).lineEvents = verified ? (req.body as any)?.events || [] : [];
  next();
}
```

Ensure body parser keeps raw body in `server/src/routes/webhook/line.ts` mount point:
```ts
import express from 'express';
import lineWebhookMiddleware from '../middleware/lineWebhook';
import { handleLineWebhook } from '../controllers/lineWebhookController';

const router = express.Router();

// Keep raw body for signature verification
router.use(express.json({
  verify: (req: any, res, buf) => { req.rawBody = buf; }
}));

router.post('/', lineWebhookMiddleware, handleLineWebhook);
export default router;
```

### 3.3 Controller (`server/src/controllers/lineWebhookController.ts`)
```ts
import { Request, Response } from 'express';
import { pool } from '../db';
import { v4 as uuidv4 } from 'uuid';

export async function handleLineWebhook(req: Request, res: Response) {
  const start = Date.now();
  const verified = !!req.lineVerified;
  const events = req.lineEvents || [];
  const deliveryId = req.get('X-Line-Delivery') || `unknown-${Date.now()}`;
  const sourceIp = req.ip || req.socket?.remoteAddress || null;
  const userAgent = req.get('User-Agent') || '';
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(req.body)).digest('hex');

  // Early reject unverified
  if (!verified) {
    await audit(req, deliveryId, 401, start, false, 'Invalid signature', events, sourceIp, userAgent, payloadHash);
    return res.status(401).json({ error: 'Invalid signature' });
  }

  try {
    for (const ev of events) {
      if (!ev.source?.userId) continue;
      // Map LINE userId -> employee (simplified; real mapping via line_user_mappings)
      const employee = await resolveEmployeeByLineId(ev.source.userId);
      if (!employee) continue;

      const tenantId = employee.tenant_id;
      const eventType = ev.type; // 'message', 'follow', etc.
      const eventAt = ev.timestamp ? new Date(ev.timestamp) : new Date();

      // Idempotent upsert: if same delivery already processed, skip business logic
      const exists = await pool.query(
        `SELECT 1 FROM punches WHERE tenant_id = $1 AND line_delivery_id = $2`,
        [tenantId, deliveryId]
      );
      if (exists.rows.length > 0) continue;

      // Determine clock action from message text or payload (simplified)
      const action = inferAction(ev);
      const now = new Date();

      await pool.query(
        `INSERT INTO punches (id, tenant_id, employee_id, event_type, event_at, line_delivery_id, created_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7)
         ON CONFLICT (tenant_id, employee_id, event_type, event_at, line_delivery_id) DO NOTHING`,
        [uuidv4(), tenantId, employee.id, action, eventAt, deliveryId, now]
      );
    }

    await audit(req, deliveryId, 200, start, true, null, events, sourceIp, userAgent, payloadHash);
    res.status(200).json({ ok: true });
  } catch (err: any) {
    await audit(req, deliveryId, 500, start, false, err.message, events, sourceIp, userAgent, payloadHash);
    res.status(500).json({ error: 'Processing failed' });
  }
}

async function audit(
  req: Request,
  deliveryId: string,
  status: number,
  start: number,
  verified: boolean,
  error: string | null,
  events: any[],
  sourceIp: string | null,
  userAgent: string,
  payloadHash: string
) {
  const tenantId = (events[0] && events[0].source?.userId) ? await resolveTenantByLineId(events[0].source.userId) : null;
  await pool.query(
    `INSERT INTO webhook_audit (id, tenant_id, event_type, line_delivery_id, employee_id, received_at, verified, source_ip, user_agent, payload_hash, processing_ms, status_code, error_message)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)`,
    [
      uuidv4(),
      tenantId,
      'line.webhook',
      deliveryId,
      null,
      new Date(),
     
