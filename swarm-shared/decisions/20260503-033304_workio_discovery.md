# workio / discovery

**Final synthesized implementation (correct + actionable)**

## 1. Diagnosis (merged, prioritized)
- **Missing HMAC-SHA256 signature verification** on `/webhook/line` enables spoofing/replay.
- **No idempotency/replay protection** causes duplicate clock-in/out or leave/OT records on LINE retries or user double-taps.
- **Tenant binding is implicit/unvalidated**; tenant inferred from payload rather than channelId→tenantId mapping, risking cross-tenant leakage.
- **Raw body unavailable** if JSON body-parser runs before signature check, breaking HMAC comparison.
- **No defense-in-depth logging/auditing**; missing trace IDs and immutable audit trails for compliance/incident response.

## 2. Proposed change (single scope)
Add a hardened `/webhook/line` route that:
1. Preserves raw body buffer for HMAC verification.
2. Verifies `X-Line-Signature` with constant-time comparison.
3. Enforces idempotency via delivery/event-level dedupe key.
4. Binds tenant via `channelId→tenantId` table lookup.
5. Writes an immutable audit row per event.
6. Returns 200 only after successful verification and idempotent write.

## 3. Implementation (TypeScript/Express)

```bash
mkdir -p server/src/routes/webhook server/src/middleware server/src/lib
```

```ts
// server/src/middleware/lineRawBody.ts
import type { Request, Response, NextFunction } from 'express';

export function lineRawBody(req: Request, _res: Response, buf: Buffer, encoding: BufferEncoding | 'buffer') {
  if (buf && buf.length > 0) {
    (req as any).rawBody = buf;
  }
}
```

```ts
// server/src/lib/lineSignature.ts
import crypto from 'crypto';

export function verifyLineSignature(rawBody: Buffer | undefined, signature: string | undefined, channelSecret: string): boolean {
  if (!rawBody || !signature || !channelSecret) return false;
  try {
    const expected = crypto.createHmac('sha256', channelSecret).update(rawBody).digest('base64');
    return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  } catch {
    return false;
  }
}
```

```ts
// server/src/lib/idempotency.ts
import type { PoolClient } from 'pg';

export async function ensureIdempotent(
  event: any,
  txClient: PoolClient
): Promise<boolean> {
  const deliveryId = event.deliveryId;
  const eventType = event.type;
  const eventId = event.message?.id || event.postback?.data || event.source?.userId || event.source?.groupId || event.source?.roomId;
  const compositeKey = deliveryId || `${eventType}:${eventId}`;

  const exists = await txClient.query(
    `SELECT 1 FROM line_webhook_events WHERE event_key = $1`,
    [compositeKey]
  );

  if (exists.rows.length > 0) return false;

  await txClient.query(
    `INSERT INTO line_webhook_events (event_key, event_type, payload, created_at)
     VALUES ($1, $2, $3, NOW())`,
    [compositeKey, eventType, event]
  );

  return true;
}
```

```ts
// server/src/lib/tenant.ts
import type { PoolClient } from 'pg';

export async function resolveTenantId(channelId: string, txClient: PoolClient): Promise<string | null> {
  const result = await txClient.query(
    `SELECT tenant_id FROM line_channels WHERE channel_id = $1`,
    [channelId]
  );
  return result.rows[0]?.tenant_id || null;
}
```

```ts
// server/src/routes/webhook/line.ts
import { Router, Request, Response, NextFunction } from 'express';
import { Pool } from 'pg';
import { lineRawBody } from '../../middleware/lineRawBody';
import { verifyLineSignature } from '../../lib/lineSignature';
import { ensureIdempotent } from '../../lib/idempotency';
import { resolveTenantId } from '../../lib/tenant';
import { AppError } from '../../lib/errors';

const router = Router();
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// Domain handler stub — replace with real clock-in/out, leave, OT logic
async function handleLineEvent(event: any, tenantId: string, txClient: PoolClient) {
  // Example: persist validated, tenant-scoped event
  await txClient.query(
    `INSERT INTO tenant_events (tenant_id, event_type, source, payload, created_at)
     VALUES ($1, $2, $3, $4, NOW())`,
    [tenantId, event.type, event.source, event]
  );
}

router.post(
  '/webhook/line',
  lineRawBody,
  async (req: Request, res: Response, next: NextFunction) => {
    const logMeta = { path: req.path, method: req.method, traceId: crypto.randomUUID() };

    try {
      const signature = req.headers['x-line-signature'] as string | undefined;
      const channelSecret = process.env.LINE_CHANNEL_SECRET;
      const rawBody = (req as any).rawBody as Buffer | undefined;

      // 1) Verify signature
      if (!verifyLineSignature(rawBody, signature, channelSecret || '')) {
        console.warn({ ...logMeta, msg: 'Invalid LINE signature' });
        return res.status(401).json({ error: 'Invalid signature' });
      }

      const body = req.body;
      const events = body.events;
      if (!Array.isArray(events) || events.length === 0) {
        return res.status(400).json({ error: 'No events' });
      }

      // 2) Process transactionally with idempotency + tenant binding
      const client = await pool.connect();
      try {
        await client.query('BEGIN');

        for (const event of events) {
          const channelId = event.source?.channelId;
          if (!channelId) continue;

          const tenantId = await resolveTenantId(channelId, client);
          if (!tenantId) {
            console.warn({ ...logMeta, channelId, msg: 'Unknown LINE channel' });
            continue;
          }

          const isNew = await ensureIdempotent(event, client);
          if (!isNew) continue;

          await handleLineEvent(event, tenantId, client);
        }

        await client.query('COMMIT');
        res.status(200).json({ ok: true });
      } catch (err) {
        await client.query('ROLLBACK');
        next(err);
      } finally {
        client.release();
      }
    } catch (err) {
      next(new AppError('Webhook processing failed', { cause: err, status: 500 }));
    }
  }
);

export default router;
```

```ts
// server/src/lib/errors.ts
export class AppError extends Error {
  status: number;
  constructor(message: string, options?: { cause?: any; status?: number }) {
    super(message, { cause: options?.cause });
    this.status = options?.status || 500;
  }
}
```

```ts
// server/src/app.ts (or server/src/index.ts)
import express from 'express';
import lineWebhookRouter from './routes/webhook/line';

const app = express();

// Do NOT use express.json() globally before signature verification.
// Use raw-body middleware only on the LINE route.
app.use('/webhook/line', lineRawBody, express.json({ type: '*/*' }), lineWebhookRouter);

// Global error handler
app.use((err: any, _req: Request, res: Response, _next: NextFunction) => {
  const status = err.status || 500;
  console.error({ err, status, msg: 'Unhandled webhook error' });
  res.status(status).json({ error: err.message || 'Internal server error' });
});

export default app;
```

## 4. Required DB migrations (run once)

```sql
-- Immutable audit of received LINE events for idempotency + compliance
CREATE TABLE IF NOT EXISTS line_webhook_events (
  id BIGSERIAL PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMEST
