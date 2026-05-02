# workio / discovery

**Final consolidated implementation (best of both proposals, reconciled for correctness + actionability)**

---

## 1. Diagnosis (merged)

- No automated tenant discovery on LINE webhook; each request must resolve tenant by channel ID (slow, noisy).
- No health/readiness endpoints for k8s/fly.io/Render — zero visibility into DB + LINE token validity at boot.
- No structured logging or request correlation IDs; debugging across tenants requires grepping stdout.
- No local dev bootstrap script; new contributors manually create DB, copy `.env`, guess schema order (>5 min friction).
- No tenant-level feature flags/config; SuperAdmin can’t toggle GPS/OT/leave per tenant without code change.
- Missing `.env.example` clarity and onboarding validation; trial-and-error config and runtime errors.
- No quick smoke test to confirm LINE webhook handshake locally.

---

## 2. Scope & constraints

- Server-side focused; minimal surface.
- Idempotent schema changes; safe for existing installs.
- ≤4 source files changed + 1 script + 1 example file + 1 smoke doc.
- Implementable in ~2 hours.

---

## 3. Implementation

### 3.1 Idempotent schema (add tenant + tenant-scoped features)

```sql:server/src/db/schema.sql
-- Tenants table
CREATE TABLE IF NOT EXISTS tenants (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  line_channel_id TEXT,
  line_secret     TEXT,
  line_access_token TEXT,
  features        JSONB NOT NULL DEFAULT '{"gps":true,"ot":true,"leave":true}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ensure users.tenant_id exists and is consistent
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='tenant_id') THEN
    ALTER TABLE users ADD COLUMN tenant_id UUID REFERENCES tenants(id);
  END IF;
END $$;

-- Index for fast channel lookup
CREATE INDEX IF NOT EXISTS idx_tenants_line_channel_id ON tenants(line_channel_id);
```

---

### 3.2 Structured logger (pino)

```ts:server/src/utils/logger.ts
import pino from 'pino';
import { Request } from 'express';

export const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  transport: process.env.NODE_ENV === 'development' ? { target: 'pino-pretty' } : undefined,
  base: { pid: process.pid },
  timestamp: pino.stdTimeFunctions.isoTime,
});

export function requestLogger(req: Request) {
  return logger.child({
    requestId: (req as any).id,
    tenantId: (req as any).tenant?.id,
    method: req.method,
    url: req.url,
  });
}
```

---

### 3.3 Tenant discovery middleware (resolve once per request)

```ts:server/src/middleware/tenantDiscovery.ts
import { Request, Response, NextFunction } from 'express';
import { pool } from '../db';
import { logger } from '../utils/logger';

declare global {
  namespace Express {
    interface Request {
      tenant?: {
        id: string;
        slug: string;
        features: Record<string, any>;
      };
    }
  }
}

export async function tenantDiscovery(
  req: Request,
  _res: Response,
  next: NextFunction
) {
  // Prefer explicit header; fallback to LINE webhook `destination`
  const channelId =
    req.header('X-Line-ChannelId') ||
    (req.body?.destination as string | undefined);

  if (!channelId) {
    return next();
  }

  try {
    const { rows } = await pool.query(
      'SELECT id, slug, features FROM tenants WHERE line_channel_id = $1 LIMIT 1',
      [channelId]
    );

    if (rows.length) {
      req.tenant = rows[0];
      // Bind tenantId for downstream loggers (best-effort)
      const reqLogger = logger.child({ tenantId: rows[0].id });
      ;(req as any).logger = reqLogger;
    }
  } catch (err) {
    logger.error({ err, channelId }, 'Tenant resolution failed');
  }

  next();
}
```

---

### 3.4 Health & readiness endpoints

```ts:server/src/routes/health.ts
import { Router } from 'express';
import { pool } from '../db';
import { logger } from '../utils/logger';

const router = Router();

router.get('/health', (_req, res) =>
  res.json({ status: 'ok', timestamp: new Date().toISOString() })
);

router.get('/ready', async (_req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ready', db: 'up' });
  } catch (err) {
    logger.error({ err }, 'DB readiness check failed');
    res.status(503).json({ status: 'unready', db: 'down' });
  }
});

export default router;
```

---

### 3.5 Local bootstrap script (one-command dev setup)

```bash:scripts/bootstrap.sh
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=> Bootstrapping Workio local dev..."

# .env stub
if [ ! -f server/.env ]; then
  if [ -f server/.env.example ]; then
    cp server/.env.example server/.env
    echo "⚠️  Created server/.env from .env.example — please review and set secrets."
  else
    echo "❌ server/.env.example not found. Create it before running bootstrap."
    exit 1
  fi
fi

# DB setup (assumes psql CLI available)
DB_NAME="${WORKIO_DB_NAME:-workio}"
if ! psql -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
  createdb "$DB_NAME" || { echo "❌ Failed to create database $DB_NAME"; exit 1; }
  echo "✅ Created database $DB_NAME"
fi

# Apply schema
psql "$DB_NAME" < server/src/db/schema.sql
echo "✅ Schema applied"

# Seed demo tenant if none exists
export PGPASSWORD="${PGPASSWORD:-postgres}"
TENANT_EXISTS=$(psql -t -A "$DB_NAME" -c "SELECT 1 FROM tenants LIMIT 1" 2>/dev/null || true)
if [ -z "$TENANT_EXISTS" ]; then
  psql "$DB_NAME" <<'EOSQL'
    INSERT INTO tenants (slug, name, line_channel_id, line_secret, features)
    VALUES ('demo', 'Demo Tenant', '1234567890', 'demo-secret', '{"gps":true,"ot":true,"leave":true}');
EOSQL
  echo "✅ Seeded demo tenant"
fi

echo "🎉 Bootstrap complete. Next steps:"
echo "  cd server && npm install && npm run dev"
```

Make executable:

```bash
chmod +x scripts/bootstrap.sh
```

---

### 3.6 `.env.example` (clear, actionable)

```env:server/.env.example
# App
NODE_ENV=development
PORT=3000

# Database (PostgreSQL)
PGHOST=localhost
PGPORT=5432
PGUSER=postgres
PGPASSWORD=postgres
WORKIO_DB_NAME=workio

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token_here
LINE_CHANNEL_SECRET=your_line_channel_secret_here

# Optional: pre-seeded tenant for local dev (matches bootstrap)
LINE_CHANNEL_ID=1234567890
```

---

### 3.7 Wire-up in app

```diff:server/src/app.ts
+import { tenantDiscovery } from './middleware/tenantDiscovery';
+import healthRoutes from './routes/health';

const app = express();

+// Structured request ID (simple)
+app.use((req, res, next) => {
+  (req as any).id = crypto.randomUUID();
+  next();
+});

+app.use(tenantDiscovery);
+app.use
