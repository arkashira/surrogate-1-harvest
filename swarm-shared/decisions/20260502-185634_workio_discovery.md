# workio / discovery

## Final Synthesis — Correct, Actionable Implementation

**Core principle**: security and data integrity first (signature verification + idempotency + audit), then observability and discoverability.  
All contradictions resolved in favor of correctness and deployable steps.

---

### 1. Corrected Diagnosis (merged + prioritized)

- **Critical security gap**: no LINE webhook signature verification → spoofing/replay possible.
- **Data integrity risk**: no idempotency for webhook deliveries → duplicate clock events.
- **Compliance/audit gap**: no structured audit trail (source, delivery ID, IP, UA) for time events.
- **Operational blind spots**:
  - No request-scoped observability (request IDs, structured logs, metrics).
  - No tenant-scoped rate limiting or abuse prevention.
  - No discoverability (catalog of LINE payloads, sample events, feature flags/introspection, health/discovery endpoints).
- **Developer experience**: missing schema docs and sample LINE payloads in repo.

---

### 2. Final Implementation Plan (single coherent set)

**Files to create/modify** (concrete paths):

- `workio/server/src/middleware/lineVerify.ts` — signature verification + delivery ID normalization.
- `workio/server/src/middleware/requestId.ts` — request ID + JSON logging.
- `workio/server/src/routes/line.ts` — idempotent webhook handler.
- `workio/server/src/db/schema.sql` — migration additions.
- `workio/server/src/index.ts` (or app.ts) — wire middleware + routes.
- `workio/docs/line-webhook.md` — payload catalog + samples.
- `workio/server/src/routes/discovery.ts` — tenant feature flags + integration status + routes list.

---

### 3. Database Migration (append to `schema.sql`)

```sql
-- Audit and idempotency fields for time_events
ALTER TABLE time_events
  ADD COLUMN IF NOT EXISTS line_delivery_id TEXT,
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'line',
  ADD COLUMN IF NOT EXISTS ip INET,
  ADD COLUMN IF NOT EXISTS ua TEXT;

-- Idempotency guard: one delivery per tenant
CREATE UNIQUE INDEX IF NOT EXISTS idx_time_events_line_delivery
  ON time_events (tenant_id, line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;

-- Optional: small lookup table for tenant-level LINE config/feature flags
CREATE TABLE IF NOT EXISTS tenant_line_config (
  tenant_id TEXT NOT NULL,
  channel_secret TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id)
);
```

---

### 4. Middleware

#### `middleware/lineVerify.ts`

```ts
import crypto from 'crypto';
import { Request, Response, NextFunction } from 'express';

const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

if (!LINE_CHANNEL_SECRET) {
  console.warn('LINE_CHANNEL_SECRET missing; webhook verification disabled (dev only)');
}

export function lineVerify(req: Request, res: Response, next: NextFunction) {
  const signature = req.headers['x-line-signature'] as string | undefined;
  if (!signature) return res.status(400).json({ error: 'Missing x-line-signature' });

  const rawBody = JSON.stringify(req.body);
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody, 'utf8')
    .digest('base64');

  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  // Normalized delivery id for idempotency
  const events = req.body?.events || [];
  const deliveryId = events[0]?.deliveryId || crypto.createHash('sha256').update(rawBody).digest('hex');
  (req as any).lineDeliveryId = deliveryId;
  (req as any).lineEvents = events;
  next();
}
```

#### `middleware/requestId.ts`

```ts
import { Request, Response, NextFunction } from 'express';
import { randomUUID } from 'crypto';

export function requestId(req: Request, res: Response, next: NextFunction) {
  const id = (req.headers['x-request-id'] as string) || randomUUID();
  (req as any).requestId = id;
  res.setHeader('x-request-id', id);
  next();
}

export function jsonLogger(req: Request, res: Response, next: NextFunction) {
  const start = Date.now();
  res.on('finish', () => {
    console.log(
      JSON.stringify({
        requestId: (req as any).requestId,
        method: req.method,
        path: req.path,
        status: res.statusCode,
        ms: Date.now() - start,
        tenantId: (req as any).tenantId || null,
        userId: (req as any).userId || null,
        lineDeliveryId: (req as any).lineDeliveryId || null,
      })
    );
  });
  next();
}
```

---

### 5. Idempotent Webhook Route

#### `routes/line.ts`

```ts
import { Router, Request, Response } from 'express';
import { lineVerify } from '../middleware/lineVerify';
import { pool } from '../db';

const router = Router();

router.post(
  '/webhook/line',
  lineVerify,
  async (
    req: Request & { lineDeliveryId?: string; lineEvents?: any[] },
    res: Response
  ) => {
    const deliveryId = req.lineDeliveryId!;
    const events = req.lineEvents || [];
    const tenantId = (req as any).tenantId; // set by tenant/auth middleware
    const userId = (req as any).userId; // resolved from LINE user mapping
    const ip = req.ip;
    const ua = req.get('User-Agent') || '';

    if (!events.length) return res.status(200).json({ ok: true });

    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // Idempotency check
      const dup = await client.query(
        `SELECT 1 FROM time_events WHERE tenant_id = $1 AND line_delivery_id = $2 LIMIT 1`,
        [tenantId, deliveryId]
      );
      if (dup.rows.length > 0) {
        await client.query('ROLLBACK');
        return res.status(200).json({ ok: true, duplicate: true });
      }

      // Process clock in/out events
      for (const ev of events) {
        if (ev.type !== 'message') continue;
        const text = (ev.message?.text || '').trim().toLowerCase();
        if (!['in', 'out'].includes(text)) continue;

        await client.query(
          `INSERT INTO time_events (tenant_id, user_id, event_type, line_delivery_id, source, ip, ua, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())`,
          [tenantId, userId, text === 'in' ? 'clock_in' : 'clock_out', deliveryId, 'line', ip, ua]
        );
      }

      await client.query('COMMIT');
      res.status(200).json({ ok: true });
    } catch (err) {
      await client.query('ROLLBACK');
      console.error(
        JSON.stringify({
          requestId: (req as any).requestId,
          error: String(err),
          deliveryId,
          tenantId,
        })
      );
      res.status(500).json({ error: 'webhook processing failed' });
    } finally {
      client.release();
    }
  }
);

export default router;
```

---

### 6. Discovery / Feature Flags Endpoint (resolves Candidate 2 gaps)

#### `routes/discovery.ts`

```ts
import { Router, Request, Response } from 'express';
import { pool } from '../db';

const router = Router();

// Lightweight discovery + tenant feature flags
router.get('/discovery', async (req: Request, res: Response) => {
  const tenantId = (req as any).tenantId || null;

  const routes = [
    { path: '/webhook/line', method: 'POST',
