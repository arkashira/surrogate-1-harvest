# workio / discovery

## 1. Diagnosis

- **No automated tenant resolution on LINE webhook** — every webhook request must perform slow/noisy runtime lookups by channel ID; no caching or fast-path.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for deployment probes, causing unreliable container restarts and slow rollouts.
- **Schema bootstrapping is runtime-dependent** — ORM/seed scripts can hide schema mismatches; no schema-first, idempotent SQL seed that works in CI/CD and local dev.
- **No tenant-scoped request context** — downstream handlers lack a reliable tenant ID in request locals, risking cross-tenant data leaks in multi-tenant queries.
- **No observability hooks** — missing request-id, structured logs, and LINE webhook signature verification logging, making debugging and replay hard.

## 2. Proposed change

Add a **fast, cached tenant resolver + health endpoints + request-scoped tenant context** to the backend webhook and API layer.

- **Files**:  
  - `workio/server/src/middleware/tenant.ts` (new)  
  - `workio/server/src/middleware/health.ts` (new)  
  - `workio/server/src/routes/webhook/line.ts` (modify)  
  - `workio/server/src/routes/api/index.ts` (modify)  
  - `workio/server/src/db/seed/001_tenant_seed.sql` (new)  
  - `workio/server/src/index.ts` (modify)

## 3. Implementation

### 3.1 Idempotent tenant seed (schema-first)

```sql
-- workio/server/src/db/seed/001_tenant_seed.sql
-- Run: psql workio < server/src/db/seed/001_tenant_seed.sql
INSERT INTO tenants (id, name, line_channel_id, timezone, created_at, updated_at)
VALUES
  ('default', 'Default Tenant', 'U1234567890abcdef', 'Asia/Bangkok', NOW(), NOW())
ON CONFLICT (line_channel_id) DO UPDATE
SET name = EXCLUDED.name,
    timezone = EXCLUDED.timezone,
    updated_at = NOW();
```

### 3.2 Tenant middleware (cached, request-scoped)

```ts
// workio/server/src/middleware/tenant.ts
import { Request, Response, NextFunction } from 'express';
import { db } from '../db';
import NodeCache from 'node-cache';

const cache = new NodeCache({ stdTTL: 300, checkperiod: 60 });

export interface Tenant {
  id: string;
  name: string;
  line_channel_id: string;
  timezone: string;
}

export interface TenantRequest extends Request {
  tenant?: Tenant;
}

export async function resolveTenant(
  req: TenantRequest,
  res: Response,
  next: NextFunction
) {
  // Prefer explicit header for API calls; fallback to LINE channel id in body
  const channelId =
    req.headers['x-line-channel-id'] as string ||
    req.body?.destination ||
    req.query?.channel_id;

  if (!channelId) {
    return res.status(400).json({ error: 'channel_id required' });
  }

  const cacheKey = `tenant:${channelId}`;
  let tenant = cache.get<Tenant>(cacheKey);

  if (!tenant) {
    const row = await db.oneOrNone<Tenant>(
      'SELECT id, name, line_channel_id, timezone FROM tenants WHERE line_channel_id = $1',
      [channelId]
    );
    if (!row) {
      return res.status(404).json({ error: 'tenant not found' });
    }
    tenant = row;
    cache.set(cacheKey, tenant);
  }

  req.tenant = tenant;
  // attach tenantId to locals/res.locals for downstream queries
  (res as any).locals = (res as any).locals || {};
  (res as any).locals.tenantId = tenant.id;
  next();
}
```

### 3.3 Health endpoints

```ts
// workio/server/src/middleware/health.ts
import { Router } from 'express';
import { db } from '../db';

export const healthRouter = Router();

healthRouter.get('/health', (_req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

healthRouter.get('/ready', async (req, res) => {
  try {
    await db.one('SELECT 1 as alive');
    res.json({ status: 'ready', db: 'up' });
  } catch (err) {
    res.status(503).json({ status: 'not ready', db: 'down', error: String(err) });
  }
});
```

### 3.4 Apply middleware and wire routes

```ts
// workio/server/src/index.ts (or app.ts)
import express from 'express';
import { healthRouter } from './middleware/health';
import { resolveTenant } from './middleware/tenant';
import lineWebhookRouter from './routes/webhook/line';
import apiRouter from './routes/api';

const app = express();
app.use(express.json());

// Health probes (no tenant required)
app.use('/health', healthRouter);

// Line webhook — tenant required
app.use('/webhook/line', resolveTenant, lineWebhookRouter);

// API routes — tenant required
app.use('/api', resolveTenant, apiRouter);

export default app;
```

### 3.5 Update LINE webhook to use tenant context

```ts
// workio/server/src/routes/webhook/line.ts
import { Router } from 'express';
import { verifySignature } from '../../lib/line-signature';
import { TenantRequest } from '../../middleware/tenant';

const router = Router();

router.post('/', async (req: TenantRequest, res) => {
  const sig = req.headers['x-line-signature'] as string;
  if (!verifySignature(JSON.stringify(req.body), sig)) {
    return res.status(401).send('Invalid signature');
  }

  const tenant = req.tenant!;
  const events = req.body.events || [];

  // Example: clock-in/out command via LINE message
  for (const ev of events) {
    if (ev.type === 'message' && ev.message.type === 'text') {
      const text = ev.message.text.trim().toLowerCase();
      if (text === 'เข้างาน' || text === 'clock in') {
        // Use tenant.id for all DB writes
        await req.app.locals.db.none(
          `INSERT INTO attendances (tenant_id, user_id, clock_in, created_at)
           VALUES ($1, $2, NOW(), NOW())`,
          [tenant.id, ev.source.userId]
        );
      }
    }
  }

  res.status(200).send('OK');
});

export default router;
```

### 3.6 Add cache dependency

```bash
cd workio/server
npm install node-cache
npm install --save-dev @types/node-cache
```

## 4. Verification

1. **Seed tenant**  
   ```bash
   psql workio < server/src/db/seed/001_tenant_seed.sql
   ```
   Verify row exists: `psql workio -c "SELECT id, line_channel_id FROM tenants;"`

2. **Health probes**  
   ```bash
   curl http://localhost:3000/health
   # {"status":"ok","timestamp":"..."}
   curl http://localhost:3000/ready
   # {"status":"ready","db":"up"}
   ```

3. **Tenant resolution (webhook)**  
   Send a test POST to `/webhook/line` with header `x-line-channel-id: U1234567890abcdef` and a valid LINE signature (or temporarily disable signature check for local test). Confirm:
   - 200 OK and attendance row created with correct `tenant_id`.
   - Cache hit on second request (check logs or cache TTL behavior).

4. **Tenant missing → 404**  
   POST with unknown `x-line-channel-id` → expect `404 {"error":"tenant not found"}`.

5. **CI readiness**  
   Add to CI script:
   ```bash
   psql workio < server/src/db/seed/001_tenant_seed.sql
   curl -f http://localhost:3000/ready
   ```
   Both must succeed for pipeline to
