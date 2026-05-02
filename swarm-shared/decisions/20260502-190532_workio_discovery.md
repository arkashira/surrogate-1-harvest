# workio / discovery

## 1. Diagnosis

- **No tenant cache on LINE webhook** — every webhook hits PostgreSQL to resolve `channelId → tenant`; slow under burst traffic and noisy under load.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness checks; deployments risk traffic sent to unhealthy pods.
- **No structured logging/request-id correlation** — hard to trace webhook flows across services and debug production issues.
- **Cold-start DB queries on every webhook** — no connection reuse or query caching; high tail latency and DB pressure.
- **No circuit-breaker or retry/backoff for LINE API calls** — transient LINE failures can cascade into lost events or retries that violate idempotency.

## 2. Proposed change

Add an in-memory tenant cache + TTL to the LINE webhook handler and expose `/health` and `/ready` endpoints. Scope:

- `workio/server/src/app.ts` — add routes and cache middleware.
- `workio/server/src/services/tenantService.ts` — add `getTenantByChannelIdCached(channelId)` with TTL (5m).
- `workio/server/src/middleware/logging.ts` — add request-id and structured logging for webhooks.
- `workio/server/src/config/cache.ts` — simple LRU/TTL cache module.

## 3. Implementation

### 3.1 Cache module (`workio/server/src/config/cache.ts`)

```ts
// Simple in-memory TTL cache for discovery/perf
export class TTLCache<K, V> {
  private store = new Map<K, { value: V; expires: number }>();

  constructor(private ttlMs: number = 5 * 60 * 1000) {}

  get(key: K): V | undefined {
    const entry = this.store.get(key);
    if (!entry) return undefined;
    if (Date.now() > entry.expires) {
      this.store.delete(key);
      return undefined;
    }
    return entry.value;
  }

  set(key: K, value: V): void {
    this.store.set(key, { value, expires: Date.now() + this.ttlMs });
  }

  delete(key: K): void {
    this.store.delete(key);
  }

  clear(): void {
    this.store.clear();
  }
}

export const tenantCache = new TTLCache<string, { id: string; name: string; timezone: string }>(5 * 60 * 1000);
```

### 3.2 Tenant service with cached lookup (`workio/server/src/services/tenantService.ts`)

```ts
import { tenantCache } from '../config/cache';
import { pool } from '../db';

export async function getTenantByChannelIdCached(channelId: string) {
  const cached = tenantCache.get(channelId);
  if (cached) return cached;

  const { rows } = await pool.query(
    `SELECT id, name, timezone FROM tenants WHERE line_channel_id = $1 AND active = true LIMIT 1`,
    [channelId]
  );

  if (rows.length === 0) return null;

  const tenant = { id: rows[0].id, name: rows[0].name, timezone: rows[0].timezone };
  tenantCache.set(channelId, tenant);
  return tenant;
}

// Call this when tenant settings change to invalidate
export function invalidateTenantCache(channelId: string) {
  tenantCache.delete(channelId);
}
```

### 3.3 Health/readiness endpoints (`workio/server/src/app.ts`)

```ts
import express from 'express';
import { pool } from './db';

export function createApp() {
  const app = express();

  // Liveness: process is alive
  app.get('/health', (_req, res) => {
    res.json({ status: 'ok', uptime: process.uptime() });
  });

  // Readiness: dependencies available
  app.get('/ready', async (_req, res) => {
    try {
      await pool.query('SELECT 1');
      res.json({ status: 'ready' });
    } catch (err) {
      res.status(503).json({ status: 'unavailable', error: String(err) });
    }
  });

  // existing routes...
  return app;
}
```

### 3.4 Structured logging middleware (`workio/server/src/middleware/logging.ts`)

```ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';

export function requestLogger(req: Request, _res: Response, next: NextFunction) {
  const requestId = req.headers['x-request-id'] || crypto.randomUUID();
  (req as any).requestId = requestId;

  const start = Date.now();
  console.log(JSON.stringify({
    level: 'info',
    requestId,
    method: req.method,
    path: req.path,
    query: req.query,
    ip: req.ip,
    userAgent: req.get('User-Agent'),
  }));

  const originalEnd = res.end;
  (res as any).end = function (...args: any[]) {
    const duration = Date.now() - start;
    console.log(JSON.stringify({
      level: 'info',
      requestId,
      method: req.method,
      path: req.path,
      status: res.statusCode,
      durationMs: duration,
    }));
    return originalEnd.apply(res, args);
  };

  next();
}
```

Wire into app:

```ts
import { requestLogger } from './middleware/logging';
app.use(requestLogger);
```

### 3.5 Use cached lookup in LINE webhook (`workio/server/src/routes/lineWebhook.ts`)

```ts
import { getTenantByChannelIdCached } from '../services/tenantService';

router.post('/webhook/line', async (req, res) => {
  const channelId = req.header('X-Line-Channel-Id') || '';
  const tenant = await getTenantByChannelIdCached(channelId);

  if (!tenant) {
    console.warn(JSON.stringify({ level: 'warn', msg: 'tenant_not_found', channelId }));
    return res.status(404).json({ error: 'tenant_not_found' });
  }

  // attach tenant to request for downstream handlers
  (req as any).tenant = tenant;

  // ... existing webhook handling
});
```

## 4. Verification

1. **Health/readiness**
   - `curl http://localhost:3000/health` → `{"status":"ok","uptime":...}`
   - Stop PostgreSQL → `curl http://localhost:3000/ready` → 503 with `{"status":"unavailable"}`; restart DB → 200.

2. **Tenant cache behavior**
   - Enable DB query logging or add a counter in `getTenantByChannelIdCached`.
   - Send two LINE webhook requests with same `X-Line-Channel-Id` within 5m; second request should hit cache (no DB query in logs).
   - Wait >5m or call `invalidateTenantCache` and verify next request queries DB.

3. **Structured logs**
   - Trigger a webhook and confirm stdout includes JSON lines with `requestId`, `method`, `path`, `status`, `durationMs`.

4. **Performance under burst**
   - Use `autocannon` or `hey` to send 50 concurrent webhooks to `/webhook/line` (mock channelId). Observe DB connections and latency: cache should keep DB queries low and p95 latency down.
