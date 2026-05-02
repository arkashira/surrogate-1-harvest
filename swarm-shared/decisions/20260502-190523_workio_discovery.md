# workio / discovery

## 1. Diagnosis

- **No tenant cache on LINE webhook** — every webhook hits PostgreSQL to resolve `channelId → tenant`; slow under burst traffic, noisy under load, and fails fast if DB is cold.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness checks; deployments risk traffic to unhealthy pods and slow rollback.
- **No structured logging/request-id** — webhook failures are hard to trace across async flows; debugging LINE events in prod is slow.
- **No circuit-breaker/retry for LINE API calls** — transient LINE API failures bubble back to users and can stall webhook processing.
- **No graceful shutdown** — in-flight webhook work is dropped on SIGTERM (common in k8s/PM2 restarts), causing missed clock events.

## 2. Proposed change

**Scope**: `workio/server/src/index.ts` (main Express app) + new `workio/server/src/lib/cache.ts` + small middleware additions.  
Goal: add tenant cache, health/ready endpoints, request-id logging, and graceful shutdown — minimal surface, high leverage.

## 3. Implementation

### 3.1 Add tenant cache (TTL 5m, refresh-on-miss)

`workio/server/src/lib/cache.ts`
```ts
import NodeCache from 'node-cache';

const tenantCache = new NodeCache({ stdTTL: 300, checkperiod: 60 });

export function getTenantId(channelId: string): string | undefined {
  return tenantCache.get<string>(channelId);
}

export function setTenantId(channelId: string, tenantId: string): void {
  tenantCache.set(channelId, tenantId);
}

export function delTenantId(channelId: string): void {
  tenantCache.del(channelId);
}
```

### 3.2 Wire cache into LINE webhook resolver

Patch the tenant resolution in `workio/server/src/index.ts` (or wherever `channelId` is resolved). Example minimal patch:

```diff
+ import { getTenantId, setTenantId } from './lib/cache';

async function resolveTenantByChannel(channelId: string): Promise<string | null> {
+   const cached = getTenantId(channelId);
+   if (cached) return cached;

  // existing DB lookup
- const row = await db.query('SELECT tenant_id FROM tenants WHERE line_channel_id = $1', [channelId]);
+ const row = await db.query('SELECT tenant_id FROM tenants WHERE line_channel_id = $1', [channelId]);
  if (row.rows.length === 0) return null;
  const tenantId = row.rows[0].tenant_id;
+ setTenantId(channelId, tenantId);
  return tenantId;
}
```

### 3.3 Add health/readiness endpoints

Append to `workio/server/src/index.ts` (after routes):

```ts
import { Router } from 'express';

const healthRouter = Router();

let isReady = false;
let lastDbCheck = 0;
const DB_CHECK_TTL = 10_000;

async function checkDb(): Promise<boolean> {
  try {
    await db.query('SELECT 1');
    return true;
  } catch {
    return false;
  }
}

// readiness: can serve traffic (db + cache ok)
healthRouter.get('/ready', async (req, res) => {
  const now = Date.now();
  if (now - lastDbCheck > DB_CHECK_TTL) {
    isReady = await checkDb();
    lastDbCheck = now;
  }
  isReady ? res.status(204).send() : res.status(503).json({ ok: false, reason: 'db_unavailable' });
});

// liveness: process alive (no external deps)
healthRouter.get('/health', (req, res) => {
  res.status(204).send();
});

app.use('/health', healthRouter);
app.use('/ready', healthRouter);
```

### 3.4 Add request-id middleware (traceability)

Insert early in middleware stack:

```ts
import crypto from 'crypto';

app.use((req, res, next) => {
  const requestId = req.headers['x-request-id'] as string || crypto.randomUUID();
  (req as any).requestId = requestId;
  res.setHeader('X-Request-ID', requestId);
  console.log(`[${requestId}] ${req.method} ${req.url}`);
  next();
});
```

### 3.5 Graceful shutdown

At server bootstrap (bottom of `index.ts`):

```ts
const server = app.listen(process.env.PORT || 3000, () => {
  console.log('Server listening');
});

let shuttingDown = false;
function gracefulShutdown(signal: string) {
  return async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`[shutdown] ${signal} received, closing server...`);
    server.close(async (err) => {
      if (err) {
        console.error('[shutdown] server close error', err);
        process.exit(1);
      }
      // optional: close db/pool here if separate
      console.log('[shutdown] closed');
      process.exit(0);
    });
    // force exit after 10s
    setTimeout(() => {
      console.error('[shutdown] forced exit');
      process.exit(1);
    }, 10_000).unref();
  };
}

process.on('SIGTERM', gracefulShutdown('SIGTERM'));
process.on('SIGINT', gracefulShutdown('SIGINT'));
```

### 3.6 Add dependency

`workio/server/package.json` — add to deps:

```json
"node-cache": "^5.1.2"
```

Then run in `workio/server`:

```bash
npm install
```

## 4. Verification

1. **Tenant cache**  
   - Start server.  
   - Trigger a LINE webhook (or simulate POST to webhook with a known `channelId`).  
   - Check logs: first request should query DB; second request with same `channelId` within 5m should skip DB (verify by log absence of DB query or by adding a debug log in resolver).

2. **Health endpoints**  
   - `curl -i http://localhost:3000/health` → 204.  
   - `curl -i http://localhost:3000/ready` → 204 when DB is up; stop PostgreSQL and within 10s it should return 503.

3. **Request-id**  
   - `curl -H "X-Request-ID: test-123" http://localhost:3000/health` → response header `X-Request-ID: test-123` and log line contains same ID.

4. **Graceful shutdown**  
   - Start server, send a request, then `kill -SIGTERM <pid>`.  
   - Verify log shows graceful shutdown message and in-flight requests complete (or are allowed a short drain) before exit.

5. **Cache invalidation (manual)**  
   - If a tenant’s LINE channel changes, call `delTenantId(channelId)` from admin operations (or restart server) — next webhook will refresh from DB.
