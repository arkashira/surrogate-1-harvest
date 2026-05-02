# workio / discovery

## 1. Diagnosis

- **No tenant cache on LINE webhook** — every webhook performs a DB lookup by `channelId`; cold DB or load spikes cause latency/timeouts and noisy logs.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness probes; deployments risk routing traffic to unhealthy pods.
- **No automated tenant bootstrap** — after `createdb workio` and schema load, there is no default tenant + SuperAdmin seed; new devs must guess manual inserts.
- **No onboarding validation script** — dev setup requires manual `.env` copy + edits and manual DB steps; leads to broken local envs and wasted time.
- **No fast-path for LINE channel → tenant** — runtime resolution on every webhook is high-cost; should resolve once at startup and cache in-memory with TTL fallback.

## 2. Proposed change

- Add `/src/server/src/routes/health.ts` — `GET /health` (liveness) and `GET /ready` (readiness; checks DB + LINE config).
- Add `/src/server/src/core/tenant-cache.ts` — in-memory `channelId→tenant` cache with 5m TTL and synchronous fallback to DB.
- Add `/src/server/src/core/bootstrap.ts` — idempotent seed: creates default tenant + SuperAdmin if none exist (safe for prod via env-gated flag).
- Add `/scripts/setup-dev.sh` — one-command local setup: copy `.env.example`, prompt for required values, create DB, run schema, run bootstrap.
- Wire cache into `/src/server/src/routes/webhook/line.ts` (or equivalent) to replace per-request DB lookups with cache-first resolution.

## 3. Implementation

### 3.1 Health routes (`/src/server/src/routes/health.ts`)

```ts
import { Router } from 'express';
import { db } from '../db';
import { config } from '../config';

const router = Router();

// Liveness: process is up
router.get('/health', (_, res) => {
  res.json({ status: 'ok', uptime: process.uptime() });
});

// Readiness: dependencies available
router.get('/ready', async (_, res) => {
  try {
    await db.query('SELECT 1');
    const hasLine = Boolean(config.line.channelAccessToken && config.line.channelSecret);
    res.json({
      status: 'ready',
      db: 'up',
      lineConfigured: hasLine,
    });
  } catch (err) {
    res.status(503).json({ status: 'unavailable', db: 'down' });
  }
});

export { router as healthRouter };
```

Register in `/src/server/src/app.ts` (or main server file):

```ts
import { healthRouter } from './routes/health';
app.use('/health', healthRouter);
app.use('/ready', healthRouter);
```

### 3.2 Tenant cache (`/src/server/src/core/tenant-cache.ts`)

```ts
import { db } from './db';

type Tenant = { id: string; name: string; channelId: string };

const CACHE_TTL_MS = 5 * 60 * 1000; // 5m
const cache = new Map<string, { tenant: Tenant | null; expiresAt: number }>();

export async function resolveTenantByChannel(channelId: string): Promise<Tenant | null> {
  const cached = cache.get(channelId);
  if (cached && cached.expiresAt > Date.now()) {
    return cached.tenant;
  }

  const row = await db.query<Tenant>(
    `SELECT id, name, "channelId" FROM tenants WHERE "channelId" = $1 LIMIT 1`,
    [channelId]
  );
  const tenant = row.rows[0] || null;
  cache.set(channelId, { tenant, expiresAt: Date.now() + CACHE_TTL_MS });
  return tenant;
}

export function clearTenantCache(channelId?: string) {
  if (channelId) cache.delete(channelId);
  else cache.clear();
}
```

### 3.3 Bootstrap seed (`/src/server/src/core/bootstrap.ts`)

```ts
import { db } from './db';
import { config } from './config';

export async function ensureDefaultTenant() {
  if (!config.bootstrap?.enable) return;

  const exists = await db.query(`SELECT 1 FROM tenants WHERE "channelId" = $1 LIMIT 1`, [
    config.line.channelId || 'default',
  ]);
  if (exists.rowCount && exists.rowCount > 0) return;

  await db.query(
    `INSERT INTO tenants (name, "channelId", "timezone", "createdAt") VALUES ($1, $2, $3, NOW())`,
    ['Default Tenant', config.line.channelId || 'default', 'Asia/Bangkok']
  );

  const tenantRow = await db.query(`SELECT id FROM tenants WHERE "channelId" = $1`, [
    config.line.channelId || 'default',
  ]);
  const tenantId = tenantRow.rows[0].id;

  // Create SuperAdmin user (auth depends on your user schema; adapt as needed)
  await db.query(
    `INSERT INTO users ("tenantId", email, name, role, "createdAt") VALUES ($1, $2, $3, $4, NOW())`,
    [tenantId, 'superadmin@workio.local', 'SuperAdmin', 'SuperAdmin']
  );
}
```

Add to `.env.example`:

```
BOOTSTRAP_ENABLE=false
```

Call once at startup (e.g., in server entrypoint):

```ts
if (process.env.NODE_ENV !== 'production' || process.env.BOOTSTRAP_ENABLE === 'true') {
  ensureDefaultTenant().catch(console.error);
}
```

### 3.4 Dev setup script (`/scripts/setup-dev.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Workio dev setup =="

# .env
if [ ! -f workio/server/.env ]; then
  echo "Creating .env from .env.example..."
  cp workio/server/.env.example workio/server/.env
  echo "Please edit workio/server/.env and set required values (DB, LINE)."
fi

# DB
echo "Ensuring database exists..."
createdb workio 2>/dev/null || true

echo "Running schema..."
psql workio < workio/server/src/db/schema.sql

echo "Bootstrapping default tenant..."
cd workio/server
BOOTSTRAP_ENABLE=true npm run dev &
SERVER_PID=$!
sleep 5
kill $SERVER_PID 2>/dev/null || true

echo "Done. Start dev servers:"
echo "  cd workio && npm run dev"
echo "  cd workio/server && npm run dev"
```

Make executable:

```bash
chmod +x /scripts/setup-dev.sh
```

### 3.5 Use cache in LINE webhook

In your LINE webhook handler (e.g., `/src/server/src/routes/webhook/line.ts`), replace per-request DB lookup:

```ts
import { resolveTenantByChannel } from '../../core/tenant-cache';

// inside webhook handler
const tenant = await resolveTenantByChannel(channelId);
if (!tenant) {
  return res.status(404).json({ error: 'tenant not found' });
}
```

## 4. Verification

1. **Health endpoints**
   - Start server: `cd workio/server && npm run dev`
   - `curl http://localhost:3000/health` → `{"status":"ok","uptime":...}`
   - `curl http://localhost:3000/ready` → `{"status":"ready","db":"up","lineConfigured":true/false}`

2. **Tenant cache**
   - In logs, confirm only one DB query per `channelId` within 5m window.
   - Use `resolveTenantByChannel` directly in REPL or a test route to verify cache hit/miss behavior.

3. **Bootstrap**
   - With `BOOTSTRAP_ENABLE=true` and empty DB, run server once; verify `tenants` and `users` have one row.
   - Re-run: no duplicate rows created (idempotent).

4. **Dev setup**
   - On a fresh environment, run `/scripts/setup-dev.sh`; confirm DB created, schema applied, and default tenant exists.

5. **Webhook fast-path**
   - Simulate two LINE events with same `channelId` within
