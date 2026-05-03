# workio / discovery

## Final Synthesis — One Correct, Actionable Plan

**Core principle**: Fix the server first (security + correctness), then add client resilience. Contradictions between candidates are resolved in favor of LINE’s documented webhook security model and production-grade idempotency (persistent, not in-memory).

---

## 1. Diagnosis (merged, prioritized)
- **Critical**: Missing `X-Line-Signature` verification allows spoofed events (attendance fraud).
- **Critical**: No idempotency → LINE retries create duplicate clock/leave/OT records.
- **High**: No event schema/timestamp validation → malformed or stale events corrupt state.
- **High**: No non-repudiation/audit (payload hash + delivery id) → hard to debug or prove tampering.
- **Medium**: Client lacks optimistic UI + rollback and local-first drafts → poor UX and data loss risk (secondary to server fixes).

---

## 2. Implementation Plan (concrete steps)

### 2.1 Install dependencies (server)
```bash
cd /opt/axentx/workio/server
npm install crypto zod lru-cache
# If multi-instance or prod, prefer Redis:
# npm install redis @upstash/redis  # choose one
```

### 2.2 Create shared types and config
File: `/opt/axentx/workio/server/src/config/line.ts`
```ts
import { config } from 'dotenv';
config();

export const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET;
if (!LINE_CHANNEL_SECRET) {
  throw new Error('LINE_CHANNEL_SECRET is required in .env');
}
```

---

### 2.3 Middleware: signature verification
File: `/opt/axentx/workio/server/src/middleware/lineWebhook.ts`
```ts
import crypto from 'crypto';
import type { NextFunction, Request, Response } from 'express';
import { LINE_CHANNEL_SECRET } from '../config/line';

export function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  const signature = req.headers['x-line-signature'] as string | undefined;
  if (!signature) {
    return res.status(400).json({ error: 'Missing X-Line-Signature' });
  }

  // Use raw body for signature if available; fallback to JSON stringify
  const body = (req as any).rawBody || JSON.stringify(req.body);
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body)
    .digest('base64');

  // Use timingSafeEqual with fixed-length buffers to avoid timing attacks
  const sigBuf = Buffer.from(signature);
  const expBuf = Buffer.from(expected);
  const len = Math.max(sigBuf.length, expBuf.length);
  const a = Buffer.alloc(len, sigBuf);
  const b = Buffer.alloc(len, expBuf);

  if (!crypto.timingSafeEqual(a, b)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }
  next();
}
```

> **Note**: If using Express body-parser, capture raw body via:
> ```ts
> app.use('/webhook/line', express.raw({ type: 'application/json' }), lineWebhookRouter);
> ```
> and parse JSON inside the route after verification.

---

### 2.4 Idempotency (production-ready)
Use a **persistent store** (Redis) for multi-instance safety. Fallback to LRU only for single-instance dev.

File: `/opt/axentx/workio/server/src/lib/idempotency.ts`
```ts
import { LRUCache } from 'lru-cache';

// Prefer Redis in production
let redisClient: any = null;

export async function initIdempotencyRedis(url?: string) {
  if (url) {
    const { Redis } = await import('@upstash/redis');
    redisClient = new Redis({ url, token: process.env.UPSTASH_REDIS_TOKEN });
  }
}

const memoryCache = new LRUCache<string, true>({ max: 50000, ttl: 1000 * 60 * 60 * 24 });

export async function isDuplicate(key: string): Promise<boolean> {
  if (redisClient) {
    const exists = await redisClient.get(key);
    if (exists) return true;
    await redisClient.set(key, '1', { ex: 60 * 60 * 24 });
    return false;
  }
  // fallback
  if (memoryCache.has(key)) return true;
  memoryCache.set(key, true);
  return false;
}
```

---

### 2.5 Schema validation
File: `/opt/axentx/workio/server/src/schemas/lineEvent.ts`
```ts
import { z } from 'zod';

export const lineEventSchema = z.object({
  destination: z.string(),
  events: z.array(
    z.object({
      type: z.string(),
      mode: z.enum(['active', 'standby']),
      timestamp: z.number().int().positive(),
      source: z.object({
        type: z.enum(['user', 'group', 'room']),
        userId: z.string().optional(),
        groupId: z.string().optional(),
        roomId: z.string().optional(),
      }),
      webhookEventId: z.string(),
      deliveryContext: z.object({ isRedelivery: z.boolean() }),
    }).passthrough()
  ),
});

export type LineEvent = z.infer<typeof lineEventSchema>;
```

---

### 2.6 Webhook route (final)
File: `/opt/axentx/workio/server/src/routes/webhook/line.ts`
```ts
import express from 'express';
import crypto from 'crypto';
import { verifyLineSignature } from '../middleware/lineWebhook';
import { isDuplicate } from '../lib/idempotency';
import { lineEventSchema } from '../schemas/lineEvent';
import { processLineEvent } from '../services/lineService';

const router = express.Router();

router.post('/line', verifyLineSignature, async (req, res) => {
  const parsed = lineEventSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({ error: 'Invalid event schema', details: parsed.error.errors });
  }

  const payload = parsed.data;
  const now = Date.now();
  const skewWindow = 1000 * 60 * 5; // 5 minutes

  for (const ev of payload.events) {
    // Replay protection
    if (Math.abs(now - ev.timestamp) > skewWindow) {
      console.warn('Stale LINE event skipped', { id: ev.webhookEventId, ts: ev.timestamp });
      continue;
    }

    // Idempotency key: use LINE's delivery ID when available
    const idemKey = `line:event:${ev.webhookEventId}`;
    if (await isDuplicate(idemKey)) {
      console.info('Duplicate LINE event skipped', { id: ev.webhookEventId });
      continue;
    }

    // Audit hash
    const payloadHash = crypto.createHash('sha256')
      .update(JSON.stringify(ev))
      .digest('hex');

    try {
      await processLineEvent({
        ...ev,
        payloadHash,
        receivedAt: new Date().toISOString(),
      });
    } catch (err) {
      console.error('Failed to process LINE event', { id: ev.webhookEventId, err });
      // Return 500 so LINE retries (idempotency prevents duplicates)
      return res.status(500).json({ error: 'Processing failed' });
    }
  }

  return res.status(200).json({ ok: true });
});

export default router;
```

---

### 2.7 Register route
In `/opt/axentx/workio/server/src/app.ts` (or equivalent):
```ts
import lineWebhookRouter from './routes/webhook/line';

// If using raw body for signature verification:
app.use('/webhook/line', express.raw({ type: 'application/json' }), lineWebhookRouter);
// Otherwise:
// app.use('/webhook/line', lineWebhookRouter);
```

---

### 2.8 Environment
`.env` (server):
```
LINE_CHANNEL_SECRET=your_line_channel_secret
# Optional for Redis-backed idempotency:
UPSTASH_REDIS_URL=https
