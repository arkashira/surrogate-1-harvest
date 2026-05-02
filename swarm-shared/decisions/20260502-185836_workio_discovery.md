# workio / discovery

## Final Synthesis — Best parts, resolved contradictions, concrete & actionable

**Guiding choices**
- Use **LRU cache** (5–10m TTL) keyed by `channelId → tenant` to protect DB on LINE webhook bursts.
- Add **`/health` and `/ready`** (DB ping + at least one tenant) for k8s/PM2.
- Add **boot validation** of required env vars and fail-fast.
- Add **safe, idempotent seed** (default tenant + SuperAdmin) with rollback.
- Centralize **tenant resolution** in middleware and reuse it in LINE webhook.
- Return **401 fast** on LINE signature failures (no stack trace/noise).
- Keep changes minimal and scoped to server entry, middleware, and seed.

---

### 1) Install lightweight dependency

```bash
cd /opt/axentx/workio/server
npm install lru-cache
```

---

### 2) Middleware: `server/src/middleware/tenant.ts`

```ts
import { Request, Response, NextFunction } from 'express';
import { Pool } from 'pg';
import LRU from 'lru-cache';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

const tenantCache = new LRU<string, { id: string; name: string; config: any }>({
  max: 500,
  ttl: 1000 * 60 * 10, // 10m (safe default)
});

export async function resolveTenant(
  req: Request,
  res: Response,
  next: NextFunction
) {
  const channelId = req.headers['x-line-channel-id'] as string;
  if (!channelId) {
    return res.status(400).json({ error: 'x-line-channel-id required' });
  }

  const cached = tenantCache.get(channelId);
  if (cached) {
    (req as any).tenant = cached;
    return next();
  }

  try {
    const { rows } = await pool.query(
      `SELECT id, name, config FROM tenants WHERE line_channel_id = $1 LIMIT 1`,
      [channelId]
    );
    if (!rows.length) {
      return res.status(404).json({ error: 'tenant not found' });
    }
    const tenant = rows[0];
    tenantCache.set(channelId, tenant);
    (req as any).tenant = tenant;
    next();
  } catch (err) {
    next(err);
  }
}

export function clearTenantCache(channelId: string) {
  tenantCache.del(channelId);
}
```

---

### 3) Boot validation + health/ready in `server/src/app.ts` (or `index.ts`)

```ts
import express, { Request, Response } from 'express';
import { Pool } from 'pg';
import { resolveTenant } from './middleware/tenant';

const app = express();
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// Fail-fast boot validation for required env
const REQUIRED_ENVS = [
  'DATABASE_URL',
  'LINE_CHANNEL_ACCESS_TOKEN',
  'LINE_CHANNEL_SECRET',
];
for (const key of REQUIRED_ENVS) {
  if (!process.env[key]) {
    console.error(`Missing required env: ${key}`);
    process.exit(1);
  }
}

// Health / readiness
app.get('/health', (_req: Request, res: Response) =>
  res.json({ status: 'ok', uptime: process.uptime() })
);

app.get('/ready', async (_req: Request, res: Response) => {
  try {
    await pool.query('SELECT 1');
    const { rows } = await pool.query('SELECT 1 FROM tenants LIMIT 1');
    if (!rows.length) return res.status(503).json({ status: 'no_tenant' });
    res.json({ status: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'db_down', error: String(err) });
  }
});

// Example usage of tenant middleware elsewhere:
// app.use('/api', resolveTenant, apiRoutes);
```

---

### 4) LINE webhook route with fast signature failure and cached tenant

`server/src/routes/lineWebhook.ts`

```ts
import express from 'express';
import line from '@line/bot-sdk';
import { resolveTenant } from '../middleware/tenant';

const config = {
  channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN || '',
  channelSecret: process.env.LINE_CHANNEL_SECRET || '',
};

const router = express.Router();

// Early signature verification using raw body
function verifyLineSignature(
  req: express.Request,
  res: express.Response,
  buf: Buffer
) {
  const signature = req.headers['x-line-signature'] as string;
  if (!line.validateSignature(buf, config.channelSecret, signature)) {
    const err: any = new Error('Invalid signature');
    err.status = 401;
    throw err;
  }
}

router.post(
  '/webhook/line',
  express.raw({ type: 'application/json', verify: verifyLineSignature }),
  (req, res, next) => {
    // Attach parsed JSON to req.body for downstream handlers while preserving rawBody
    try {
      (req as any).rawBody = req.body;
      (req as any).body = JSON.parse(req.body.toString());
      next();
    } catch (err: any) {
      err.status = 400;
      next(err);
    }
  },
  resolveTenant,
  async (req: express.Request & { rawBody?: Buffer; body?: any }, res: express.Response) => {
    try {
      const events = (req.body && req.body.events) || [];
      const tenant = (req as any).tenant;

      // Process events with tenant context
      for (const ev of events) {
        // handle ev with tenant.id
        // e.g., queue or handle message
      }

      res.status(200).send('OK');
    } catch (err: any) {
      const status = err.status || 500;
      res.status(status).json({ error: err.message });
    }
  }
);

export default router;
```

Wire into `app.ts`:

```ts
import lineWebhook from './routes/lineWebhook';
app.use('/webhook', lineWebhook);
```

---

### 5) Idempotent seed script: `server/src/setup/seed.ts`

```ts
import { Pool } from 'pg';
import * as crypto from 'crypto';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

async function seed() {
  await pool.query('BEGIN');
  try {
    const { rows } = await pool.query('SELECT id FROM tenants LIMIT 1');
    if (rows.length > 0) {
      console.log('Seed skipped: tenant already exists');
      await pool.query('COMMIT');
      await pool.end();
      return;
    }

    const tenantId = crypto.randomUUID();
    const adminId = crypto.randomUUID();
    const lineChannelId = process.env.LINE_CHANNEL_ID || 'demo-channel';

    await pool.query(
      `INSERT INTO tenants (id, name, line_channel_id, config)
       VALUES ($1, $2, $3, $4)`,
      [tenantId, 'Default Tenant', lineChannelId, '{}']
    );

    // Minimal safe insert for SuperAdmin; adapt columns to your schema
    await pool.query(
      `INSERT INTO users (id, tenant_id, email, name, role, password_hash, is_active)
       VALUES ($1, $2, $3, $4, $5, $6, true)`,
      [adminId, tenantId, 'admin@workio.local', 'SuperAdmin', 'SuperAdmin', '']
    );

    await pool.query('COMMIT');
    console.log('Seed complete: default tenant + SuperAdmin created');
  } catch (err) {
    await pool.query('ROLLBACK');
    console.error('Seed failed:', err);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

if (require.main === module) {
  seed();
}
```

Add npm script in `server/package.json`:

```json
"scripts": {
  "seed": "ts
