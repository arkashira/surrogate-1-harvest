# workio / discovery

## 1. Diagnosis

- No automated onboarding/setup validation: new devs must manually create DB, run schema, copy `.env`, and guess correct values; leads to broken local envs and wasted time.
- No tenant bootstrap: after DB is created, there is no default tenant/SuperAdmin seed, so the app starts empty and is unusable until someone runs manual SQL inserts.
- No health-check or readiness endpoint: no `/health` or `/ready` route to confirm DB + LINE config connectivity; makes deployment/debugging blind.
- No LINE webhook signature verification middleware: webhook endpoint accepts requests without validating `X-Line-Signature`, risking spoofed clock-in events.
- No simple discovery script to surface current project state (env health, DB connectivity, migrations status) for ops/on-call.

## 2. Proposed change

Add a compact setup/validation module and a health endpoint that can be implemented in <2h:

- `server/src/middleware/line-verify.ts` – verify `X-Line-Signature`
- `server/src/routes/health.ts` – `GET /health` (DB ping, LINE config presence, env sanity)
- `server/src/db/seed.ts` – create default tenant + SuperAdmin user if none exist
- `scripts/setup-dev.sh` – idempotent local bootstrap (checks/creates DB, runs schema, seeds, verifies)
- Wire middleware into `server/src/index.ts` and mount `/health` before other routes.

## 3. Implementation

### 3.1 LINE signature verification middleware

`server/src/middleware/line-verify.ts`
```ts
import crypto from 'crypto';
import { Request, Response, NextFunction } from 'express';

export function lineVerify(req: Request, res: Response, next: NextFunction) {
  const channelSecret = process.env.LINE_CHANNEL_SECRET;
  const signature = req.get('X-Line-Signature');

  if (!channelSecret) {
    console.warn('LINE_CHANNEL_SECRET not set; skipping signature verification');
    return next();
  }

  if (!signature) {
    return res.status(400).json({ error: 'Missing X-Line-Signature' });
  }

  const body = JSON.stringify(req.body);
  const hash = crypto
    .createHmac('sha256', channelSecret)
    .update(body, 'utf8')
    .digest('base64');

  if (hash !== signature) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  next();
}
```

### 3.2 Health check route

`server/src/routes/health.ts`
```ts
import { Router } from 'express';
import { Pool } from 'pg';

const router = Router();

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

router.get('/health', async (req, res) => {
  const checks: Record<string, any> = {
    uptime: process.uptime(),
    timestamp: new Date().toISOString(),
    env: {
      DATABASE_URL: !!process.env.DATABASE_URL,
      LINE_CHANNEL_ACCESS_TOKEN: !!process.env.LINE_CHANNEL_ACCESS_TOKEN,
      LINE_CHANNEL_SECRET: !!process.env.LINE_CHANNEL_SECRET,
    },
  };

  // DB connectivity
  try {
    await pool.query('SELECT 1');
    checks.database = { status: 'ok' };
  } catch (err: any) {
    checks.database = { status: 'error', message: err.message };
  } finally {
    await pool.end();
  }

  const healthy = checks.database?.status === 'ok';
  res.status(healthy ? 200 : 503).json(checks);
});

export default router;
```

### 3.3 Seed default tenant + SuperAdmin

`server/src/db/seed.ts`
```ts
import { Pool } from 'pg';
import * as crypto from 'crypto';

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

async function seed() {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Ensure tenant exists
    const tenantRes = await client.query(
      `INSERT INTO tenants (name, domain, settings)
       VALUES ('Default Tenant', 'default', '{}')
       ON CONFLICT (domain) DO UPDATE SET name = EXCLUDED.name
       RETURNING id`
    );
    const tenantId = tenantRes.rows[0].id;

    // Ensure SuperAdmin exists (simple email-based lookup)
    const email = 'superadmin@workio.local';
    const existing = await client.query('SELECT id FROM users WHERE email = $1', [email]);
    if (existing.rowCount === 0) {
      const passwordHash = crypto.createHash('sha256').update('ChangeMe123!').digest('hex');
      await client.query(
        `INSERT INTO users (tenant_id, email, password_hash, name, role)
         VALUES ($1, $2, $3, $4, $5)`,
        [tenantId, email, passwordHash, 'SuperAdmin', 'SuperAdmin']
      );
      console.log('Seeded SuperAdmin:', email);
    } else {
      console.log('SuperAdmin already exists');
    }

    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
    await pool.end();
  }
}

if (require.main === module) {
  seed()
    .then(() => process.exit(0))
    .catch((err) => {
      console.error(err);
      process.exit(1);
    });
}
```

### 3.4 Idempotent dev setup script

`scripts/setup-dev.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Workio dev setup =="

# .env presence
if [ ! -f server/.env ]; then
  if [ -f server/.env.example ]; then
    cp server/.env.example server/.env
    echo "✓ copied .env.example to .env (edit as needed)"
  else
    echo "✗ missing .env.example" >&2
    exit 1
  fi
fi

# DB
export $(grep -v '^#' server/.env | xargs) || true
DB_NAME="workio"
if ! psql -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
  createdb "$DB_NAME"
  echo "✓ created database $DB_NAME"
else
  echo "✓ database $DB_NAME exists"
fi

# Schema
psql "$DB_NAME" < server/src/db/schema.sql
echo "✓ schema applied"

# Seed
cd server
npm run build -- --project src/db/seed.ts 2>/dev/null || true
node -r ts-node/register src/db/seed.ts
cd ..
echo "✓ seed complete"

# Quick health check
echo "== Health check =="
curl -s http://localhost:3000/health | jq . || echo "Start backend to check /health"
```

Make executable:
```bash
chmod +x scripts/setup-dev.sh
```

### 3.5 Wire into server entrypoint

`server/src/index.ts` (minimal diff)
```diff
+import lineVerify from './middleware/line-verify';
+import healthRoute from './routes/health';
+
 const app = express();

+app.use(express.json({ limit: '1mb' }));
+app.use('/health', healthRoute);
+
+// Apply LINE verification to webhook route only
+app.post('/webhook/line', lineVerify, lineRouter);
+
 // ... rest of routes
```

## 4. Verification

1. Run setup:
   ```bash
   cd /opt/axentx/workio
   ./scripts/setup-dev.sh
   ```
   Expect: database created, schema applied, SuperAdmin seeded, no errors.

2. Start backend:
   ```bash
   cd server && npm run dev
   ```
   In another terminal:
   ```bash
   curl http://localhost:3000/health | jq
   ```
   Expect: `{"database":{"status":"ok"},...}` with 200.

3. Validate LINE signature rejection (simulate):
   ```bash
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application
