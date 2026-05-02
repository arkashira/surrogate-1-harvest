# workio / discovery

## Final synthesized implementation (correct + actionable)

This merges the strongest, non-redundant parts of both proposals and resolves conflicts in favor of correctness, security, and concrete actionability.

---

### 1. Diagnosis (merged)

- No tenant cache on LINE webhook → every webhook hits DB to resolve `channelId → tenant`; slow, noisy, and fails fast under load or cold DB.
- Missing `/health` and `/ready` endpoints → k8s/PM2/liveness probes can’t detect unhealthy pods; deployments risk routing traffic too early.
- No automated tenant bootstrap or dev onboarding → after DB creation there is no default tenant/SuperAdmin seed; first login fails and onboarding is manual.
- No setup validation script → devs manually create DB, copy `.env`, guess values; leads to broken local envs and wasted time.
- No fast-path for LINE signature verification → webhook does DB lookups before validating LINE signature; opens unnecessary DB load for invalid requests.

---

### 2. Scope and files (minimal, focused)

- `server/src/middleware/tenant.ts` — in-memory LRU tenant cache with 5m TTL and optional stale-while-revalidate.
- `server/src/middleware/line.ts` — verify LINE signature **before** tenant resolution and before parsing heavy JSON.
- `server/src/routes/health.ts` — `/health` (liveness) and `/ready` (readiness) endpoints.
- `server/src/db/seed.ts` — idempotent default tenant + SuperAdmin seed.
- `server/src/scripts/setup-dev.ts` — automated dev onboarding (create `.env`, validate DB, run seed).
- `server/src/index.ts` — mount routes and wire middleware in correct order.
- `server/package.json` — add `seed` and `setup:dev` scripts.

---

### 3. Implementation

#### 3.1 Tenant cache middleware (`server/src/middleware/tenant.ts`)

```ts
// server/src/middleware/tenant.ts
import { Request, Response, NextFunction } from 'express';
import { db } from '../db/index.js';
import { tenants } from '../db/schema.js';
import { eq } from 'drizzle-orm';

interface Tenant {
  id: string;
  name: string;
  channelId: string;
  settings?: Record<string, unknown>;
  createdAt: Date;
}

type CacheEntry = {
  tenant: Tenant;
  expires: number;
  stale?: boolean;
};

const CACHE_TTL_MS = 5 * 60 * 1000; // 5m
const STALE_REVALIDATE_MS = 60 * 1000; // allow 1m stale while revalidate
const cache = new Map<string, CacheEntry>();

function isStale(entry: CacheEntry, now: number) {
  return entry.expires - now <= STALE_REVALIDATE_MS;
}

export async function resolveTenant(
  req: Request,
  res: Response,
  next: NextFunction
) {
  // Accept channelId from LINE webhook body (group or user)
  const channelId =
    req.body?.events?.[0]?.source?.groupId ||
    req.body?.events?.[0]?.source?.userId ||
    req.query.channelId;

  if (!channelId || typeof channelId !== 'string') {
    return res.status(400).json({ error: 'channelId missing or invalid' });
  }

  const now = Date.now();
  const cached = cache.get(channelId);

  // Fast path: valid cache
  if (cached && cached.expires > now) {
    res.locals.tenant = cached.tenant;
    // Async refresh if stale (non-blocking)
    if (isStale(cached, now)) {
      refreshTenant(channelId).catch(() => {/* best-effort */});
    }
    return next();
  }

  // Stale-while-revalidate: serve stale while fetching
  if (cached && cached.stale) {
    res.locals.tenant = cached.tenant;
    refreshTenant(channelId).catch(() => {/* best-effort */});
    return next();
  }

  // Miss: fetch from DB
  try {
    await refreshTenant(channelId);
    const entry = cache.get(channelId);
    if (!entry) {
      return res.status(404).json({ error: 'tenant not found' });
    }
    res.locals.tenant = entry.tenant;
    next();
  } catch (err) {
    next(err);
  }
}

async function refreshTenant(channelId: string) {
  const rows = await db.select().from(tenants).where(eq(tenants.channelId, channelId));
  if (!rows.length) {
    cache.delete(channelId);
    throw new Error('tenant not found');
  }
  const tenant = rows[0];
  const now = Date.now();
  cache.set(channelId, {
    tenant,
    expires: now + CACHE_TTL_MS,
    stale: false,
  });
  return tenant;
}

export function invalidateTenantCache(channelId: string) {
  cache.delete(channelId);
}
```

---

#### 3.2 LINE signature-first middleware (`server/src/middleware/line.ts`)

```ts
// server/src/middleware/line.ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';

const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

export function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  const signature = req.headers['x-line-signature'] as string | undefined;
  if (!signature) {
    return res.status(401).json({ error: 'missing x-line-signature' });
  }

  // Use raw body if available (recommended for signature verification).
  // If rawBody is not populated by middleware, fallback to JSON stringify (less safe).
  const body = (req as any).rawBody || JSON.stringify(req.body || {});
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(Buffer.isBuffer(body) ? body : Buffer.from(String(body), 'utf8'))
    .digest('base64');

  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    return res.status(401).json({ error: 'invalid signature' });
  }

  next();
}
```

> **Note**: For production, add middleware to capture raw body before JSON parsing on the LINE webhook route only.

---

#### 3.3 Health routes (`server/src/routes/health.ts`)

```ts
// server/src/routes/health.ts
import { Router } from 'express';
import { db } from '../db/index.js';

export const healthRouter = Router();

healthRouter.get('/health', (_, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

healthRouter.get('/ready', async (_, res) => {
  try {
    // Lightweight query to verify DB connectivity
    await db.execute({ sql: 'SELECT 1' });
    res.json({ status: 'ready', db: 'connected' });
  } catch (err) {
    res.status(503).json({ status: 'not ready', db: 'disconnected' });
  }
});
```

---

#### 3.4 Idempotent seed (`server/src/db/seed.ts`)

```ts
// server/src/db/seed.ts
import { db } from './index.js';
import { tenants, users } from './schema.js';
import { eq } from 'drizzle-orm';
import { hash } from 'bcryptjs';

export async function seed() {
  // Default tenant
  const existingTenant = await db.select().from(tenants).where(eq(tenants.name, 'Default'));
  let tenantId: string;

  if (!existingTenant.length) {
    const inserted = await db
      .insert(tenants)
      .values({
        name: 'Default',
        channelId: 'default-channel',
        settings: { currency: 'THB', timezone: 'Asia/Bangkok' },
      })
      .returning({ id: tenants.id });
    tenantId = inserted[0].id;
    console.log('Created default tenant:', tenantId);
  } else {
    tenantId = existingTenant[0].id;
    console.log('Default tenant exists:', tenantId);
  }

  // SuperAdmin user
 
