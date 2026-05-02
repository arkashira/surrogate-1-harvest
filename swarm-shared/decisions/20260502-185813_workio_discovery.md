# workio / discovery

## 1. Diagnosis

- **No tenant cache on LINE webhook** — every webhook hits the DB to resolve `channelId → tenant`; slow, noisy, and fails fast under load or cold DB.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness checks; deployments risk routing traffic to unhealthy pods.
- **No automated dev onboarding bootstrap** — new devs must manually create DB, run schema, copy `.env`, and guess values; leads to broken local envs and wasted time.
- **No default tenant/SuperAdmin seed** — after DB creation there is no seeded tenant or admin user; first-login friction and manual SQL required.
- **No fast-path for LINE signature verification failures** — unhandled or noisy failures on malformed/expired signatures create log spam and no clear observability.

## 2. Proposed change

Add a lightweight, in-memory tenant cache + health endpoints + dev bootstrap script scoped to:

- `workio/server/src/middleware/tenantCache.ts` (new)
- `workio/server/src/routes/health.ts` (new)
- `workio/server/src/db/seed.ts` (new)
- `workio/server/src/app.ts` (wireup)
- `workio/server/package.json` (add dev script)

## 3. Implementation

### 3.1 Tenant cache middleware

```ts
// workio/server/src/middleware/tenantCache.ts
import { Request, Response, NextFunction } from 'express';
import { getDb } from '../db/index.js';

interface Tenant {
  id: string;
  name: string;
  channelId: string;
  channelSecret: string;
  channelAccessToken: string;
}

const CACHE_TTL_MS = 5 * 60 * 1000; // 5m
const cache = new Map<string, { tenant: Tenant; expiresAt: number }>();

export async function resolveTenant(
  req: Request & { tenant?: Tenant },
  res: Response,
  next: NextFunction
) {
  const channelId = req.body?.destination ?? req.header('x-line-channel-id');
  if (!channelId) {
    return res.status(400).json({ error: 'Missing channel identifier' });
  }

  const now = Date.now();
  const cached = cache.get(channelId);
  if (cached && cached.expiresAt > now) {
    req.tenant = cached.tenant;
    return next();
  }

  try {
    const db = getDb();
    const row = await db.get<Tenant>(
      `SELECT id, name, channelId, channelSecret, channelAccessToken
       FROM tenants WHERE channelId = ? LIMIT 1`,
      [channelId]
    );

    if (!row) {
      return res.status(404).json({ error: 'Tenant not found' });
    }

    const tenant = row;
    cache.set(channelId, { tenant, expiresAt: now + CACHE_TTL_MS });
    req.tenant = tenant;
    next();
  } catch (err) {
    console.error('[tenantCache] DB resolve failed', err);
    res.status(500).json({ error: 'Tenant resolution failed' });
  }
}

export function clearTenantCache(channelId?: string) {
  if (channelId) {
    cache.delete(channelId);
  } else {
    cache.clear();
  }
}
```

### 3.2 Health/readiness endpoints

```ts
// workio/server/src/routes/health.ts
import { Router } from 'express';
import { getDb } from '../db/index.js';

const router = Router();

router.get('/health', (_req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

router.get('/ready', async (_req, res) => {
  try {
    const db = getDb();
    // lightweight query to verify DB connectivity
    await db.get('SELECT 1 as ok');
    res.json({ status: 'ready', db: 'connected' });
  } catch (err) {
    res.status(503).json({ status: 'unready', db: 'disconnected', error: String(err) });
  }
});

export default router;
```

### 3.3 Dev bootstrap/seed script

```ts
// workio/server/src/db/seed.ts
import { getDb } from './index.js';
import * as crypto from 'crypto';
import * as argon2 from 'argon2';

async function seed() {
  const db = getDb();
  console.log('[seed] ensuring default tenant + superadmin');

  // Default tenant
  const channelId = process.env.DEFAULT_CHANNEL_ID || 'demo-channel';
  const existing = await db.get('SELECT id FROM tenants WHERE channelId = ?', [channelId]);
  if (!existing) {
    await db.run(
      `INSERT INTO tenants (id, name, channelId, channelSecret, channelAccessToken, createdAt)
       VALUES (?, ?, ?, ?, ?, datetime('now'))`,
      [
        crypto.randomUUID(),
        'Default Tenant',
        channelId,
        process.env.DEFAULT_CHANNEL_SECRET || 'secret',
        process.env.DEFAULT_CHANNEL_ACCESS_TOKEN || 'token',
      ]
    );
    console.log('[seed] default tenant created');
  }

  // SuperAdmin user
  const adminExists = await db.get('SELECT id FROM users WHERE role = "SuperAdmin" LIMIT 1');
  if (!adminExists) {
    const password = process.env.DEFAULT_ADMIN_PASSWORD || 'Admin123!';
    const hashed = await argon2.hash(password);
    await db.run(
      `INSERT INTO users (id, email, name, passwordHash, role, tenantId, createdAt)
       VALUES (?, ?, ?, ?, 'SuperAdmin',
         (SELECT id FROM tenants WHERE channelId = ? LIMIT 1),
         datetime('now'))`,
      [crypto.randomUUID(), 'admin@workio.local', 'SuperAdmin', hashed, channelId]
    );
    console.log('[seed] SuperAdmin created (email: admin@workio.local)');
  }

  console.log('[seed] done');
}

if (require.main === module) {
  seed().catch((err) => {
    console.error('[seed] failed', err);
    process.exit(1);
  });
}
```

### 3.4 Wireup in app

```ts
// workio/server/src/app.ts (additions)
import { resolveTenant } from './middleware/tenantCache.js';
import healthRoutes from './routes/health.js';

// Add before LINE webhook routes
app.use('/webhook/line', resolveTenant);

// Health endpoints (no tenant required)
app.use('/health', healthRoutes);
app.use('/ready', healthRoutes);
```

### 3.5 Package scripts

```json
// workio/server/package.json (add)
{
  "scripts": {
    "dev": "tsx watch src/index.ts",
    "seed": "tsx src/db/seed.ts",
    "start": "node dist/index.js"
  }
}
```

## 4. Verification

1. **Tenant cache**
   - Start server: `cd workio/server && npm run dev`
   - Send a LINE-like POST to `/webhook/line` twice with same `destination` (channelId).
   - Observe logs: first request resolves via DB; second hits cache (no DB query logged).

2. **Health/readiness**
   - `curl http://localhost:3000/health` → `{"status":"ok",...}`
   - `curl http://localhost:3000/ready` → `{"status":"ready","db":"connected"}`
   - Stop PostgreSQL and re-hit `/ready` → `503` with `db: disconnected`.

3. **Dev bootstrap**
   - Create `.env` with minimal vars or rely on defaults.
   - Run `cd workio/server && npm run seed`
   - Verify output: “default tenant created” and “SuperAdmin created”.
   - Query DB: `sqlite3 workio.db "SELECT email,role FROM users;"` → shows admin@workio.local / SuperAdmin.

4. **Cache invalidation (optional)**
   - Call internal helper `clearTenantCache(channelId)` from any admin route or REPL to force refresh; next webhook for that channelId should re-query DB.
