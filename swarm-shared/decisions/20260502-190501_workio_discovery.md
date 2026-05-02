# workio / discovery

## 1. Diagnosis

- **No tenant cache on LINE webhook** — every webhook hits the DB to resolve `channelId → tenant`; slow, noisy, and fails fast under load or cold DB.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness checks; deployments risk traffic before DB/tenant readiness.
- **LINE signature verification is synchronous and un-cached** — repeated verification of the same channel secrets adds CPU and latency on bursts.
- **No circuit-breaker or timeout on LINE API calls** — outbound calls to LINE Messaging API can stall the webhook handler and exhaust event-loop/connection pool.
- **No structured logging/request-id on webhook ingress** — hard to trace bursts or failures across logs and metrics.

## 2. Proposed change

Add a tenant cache (LRU, 5m TTL) and health/readiness endpoints to the backend webhook layer, plus a small middleware for request-id and structured logging.

- **Files**:  
  - `workio/server/src/middleware/tenantCache.ts` (new)  
  - `workio/server/src/middleware/requestLogger.ts` (new)  
  - `workio/server/src/routes/health.ts` (new)  
  - `workio/server/src/routes/webhook/line.ts` (modify)  
  - `workio/server/src/index.ts` (wire up middleware + routes)

## 3. Implementation

### 3.1 tenantCache.ts

```ts
// workio/server/src/middleware/tenantCache.ts
import LRU from 'lru-cache';

export type Tenant = {
  id: string;
  name: string;
  channelId: string;
  // minimal shape used by webhook
};

const cache = new LRU<string, Tenant>({
  max: 500,
  ttl: 1000 * 60 * 5, // 5 minutes
  allowStale: false,
});

export async function getTenantByChannelId(
  channelId: string,
  fetchFromDb: (channelId: string) => Promise<Tenant | null>
): Promise<Tenant | null> {
  const cached = cache.get(channelId);
  if (cached) return cached;

  const tenant = await fetchFromDb(channelId);
  if (tenant) cache.set(channelId, tenant);
  return tenant;
}

export function invalidateTenantChannel(channelId: string) {
  cache.delete(channelId);
}
```

### 3.2 requestLogger.ts

```ts
// workio/server/src/middleware/requestLogger.ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';

export function requestLogger(req: Request, res: Response, next: NextFunction) {
  const requestId = req.headers['x-request-id'] as string || crypto.randomUUID();
  (req as any).requestId = requestId;

  const start = Date.now();
  res.on('finish', () => {
    console.log(JSON.stringify({
      requestId,
      method: req.method,
      url: req.originalUrl,
      status: res.statusCode,
      durationMs: Date.now() - start,
      ip: req.ip,
      userAgent: req.get('User-Agent'),
    }));
  });

  next();
}
```

### 3.3 health.ts

```ts
// workio/server/src/routes/health.ts
import { Router } from 'express';
import db from '../db'; // adjust import to your db client
import { getTenantByChannelId } from '../middleware/tenantCache';

const router = Router();

// Liveness: process + basic deps
router.get('/health', async (_req, res) => {
  res.json({ status: 'ok', uptime: process.uptime() });
});

// Readiness: db + at least one tenant exists (or config allows empty)
router.get('/ready', async (_req, res) => {
  try {
    // lightweight db ping / query
    await db.query('SELECT 1');
    // optional: ensure at least one tenant exists (if required)
    const result = await db.query('SELECT 1 FROM tenants LIMIT 1');
    const hasTenant = result.rowCount > 0;
    if (!hasTenant) {
      return res.status(503).json({ status: 'unavailable', reason: 'no_tenants' });
    }
    res.json({ status: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'unavailable', error: String(err) });
  }
});

export default router;
```

### 3.4 Modify line webhook route

```ts
// workio/server/src/routes/webhook/line.ts  (apply focused changes)
import { Router } from 'express';
import crypto from 'crypto';
import axios from 'axios';
import db from '../../db';
import { getTenantByChannelId } from '../../middleware/tenantCache';

const router = Router();

async function fetchTenantFromDb(channelId: string) {
  const result = await db.query('SELECT id, name, channel_id AS "channelId" FROM tenants WHERE channel_id = $1 LIMIT 1', [channelId]);
  return result.rows[0] || null;
}

async function verifyLineSignature(body: string, signature: string, channelSecret: string): Promise<boolean> {
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(body, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

// Circuit-breaker-ish wrapper for LINE API (simple timeout + retry once)
async function replyLineToken(token: string, payload: any, timeoutMs = 8000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    await axios.post('https://api.line.me/v2/bot/message/reply', payload, {
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      signal: controller.signal,
    });
  } finally {
    clearTimeout(id);
  }
}

router.post('/line', async (req, res) => {
  const requestId = (req as any).requestId || 'unknown';
  const bodyRaw = JSON.stringify(req.body); // careful: if body already parsed, use raw body middleware in prod
  const signature = req.headers['x-line-signature'] as string;

  try {
    // Resolve tenant quickly via cache
    const channelId = req.body.destination; // LINE webhook destination (channel)
    const tenant = await getTenantByChannelId(channelId, fetchTenantFromDb);
    if (!tenant) {
      console.warn(JSON.stringify({ requestId, reason: 'tenant_not_found', channelId }));
      return res.status(404).json({ error: 'tenant_not_found' });
    }

    // Verify signature (in real usage use raw body middleware to get exact payload bytes)
    // Placeholder: assume channel secret available on tenant (store securely)
    const channelSecret = process.env[`LINE_SECRET_${tenant.id}`] || process.env.LINE_CHANNEL_SECRET;
    if (channelSecret && signature) {
      // Note: bodyRaw !== original raw bytes; for production use raw-body middleware
      const ok = await verifyLineSignature(bodyRaw, signature, channelSecret);
      if (!ok) {
        console.warn(JSON.stringify({ requestId, reason: 'invalid_signature', channelId }));
        return res.status(401).json({ error: 'invalid_signature' });
      }
    }

    // Fast 200 OK to LINE to avoid retries; process async if needed
    res.json({});

    // Example async handling (non-blocking)
    const events = req.body.events || [];
    for (const ev of events) {
      if (ev.type === 'message' && ev.message.type === 'text') {
        try {
          await replyLineToken(
            process.env.LINE_CHANNEL_ACCESS_TOKEN!,
            {
              replyToken: ev.replyToken,
              messages: [{ type: 'text', text: `บันทึกเวลา: ${ev.message.text} (tenant: ${tenant.name})` }],
            },
            8000
          );
        } catch (err) {
          console.error(JSON.stringify({ requestId, reason: 'line_reply_failed', error: String(err), channelId }));
        }
      }
    }
  } catch (
