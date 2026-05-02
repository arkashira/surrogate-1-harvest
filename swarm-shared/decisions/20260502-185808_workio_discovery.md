# workio / discovery

## 1. Diagnosis

- **No tenant resolution cache on LINE webhook** — every webhook hits the DB to resolve `channelId → tenantId`; slow, noisy, and fails fast under load or when DB is cold.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness checks; deployments risk traffic before the app is actually usable.
- **No automated tenant bootstrap** — after DB is created there is no default tenant + SuperAdmin seed; new devs and CI environments start with an empty unusable system.
- **No setup validation script** — devs must manually create DB, run schema, copy `.env`, and guess values; leads to broken local envs and wasted onboarding time.
- **No fast-path for LINE signature verification metadata** — runtime re-fetches channel secret on every webhook instead of caching tenant config.

## 2. Proposed change

Add a lightweight **tenant-aware health probe + bootstrap seed + LINE webhook tenant cache** scoped to:

- `server/src/app.ts` — add `/health` and `/ready` routes (express)
- `server/src/services/tenantService.ts` — add `getTenantByChannelIdCached(channelId)` with in-memory TTL cache (5m)
- `server/src/db/seeds/01_default_tenant.sql` + `server/src/db/seeds/seed.ts` — default tenant + SuperAdmin seed
- `scripts/setup-dev.sh` — idempotent local setup (create db, run schema, seed, verify .env)

## 3. Implementation

### 3.1 Health/readiness endpoints (`server/src/app.ts`)

```ts
// Add near top with other imports
import { Router } from 'express';

const healthRouter = Router();

// Liveness: process is up
healthRouter.get('/health', (req, res) => {
  res.json({ status: 'ok', uptime: process.uptime() });
});

// Readiness: db + LINE config minimally available
healthRouter.get('/ready', async (req, res) => {
  try {
    const ok = await db.$queryRaw`SELECT 1`;
    const hasLineConfig = !!process.env.LINE_CHANNEL_ACCESS_TOKEN;
    if (ok && hasLineConfig) {
      return res.json({ status: 'ready', db: 'up', lineConfig: 'present' });
    }
    return res.status(503).json({ status: 'not_ready', db: !!ok, lineConfig: !!hasLineConfig });
  } catch (err) {
    return res.status(503).json({ status: 'not_ready', error: String(err) });
  }
});

// Mount before API routes
app.use('/health', healthRouter);
```

### 3.2 Tenant cache service (`server/src/services/tenantService.ts`)

```ts
// Add to existing file or create
const TTL_MS = 5 * 60 * 1000; // 5m
const cache = new Map<string, { tenantId: string; expiresAt: number }>();

export async function getTenantByChannelIdCached(channelId: string) {
  const now = Date.now();
  const entry = cache.get(channelId);
  if (entry && entry.expiresAt > now) {
    return entry.tenantId;
  }

  const tenant = await db.tenant.findFirst({
    where: { lineChannelId: channelId },
    select: { id: true },
  });

  if (!tenant) throw new Error(`Tenant not found for channelId=${channelId}`);

  cache.set(channelId, { tenantId: tenant.id, expiresAt: now + TTL_MS });
  return tenant.id;
}

// Optional: clear cache on tenant update/delete
export function invalidateTenantCache(channelId: string) {
  cache.delete(channelId);
}
```

Use in LINE webhook handler (example):

```ts
// server/src/routes/lineWebhook.ts
import { getTenantByChannelIdCached } from '../services/tenantService';

router.post('/webhook/line', async (req, res) => {
  const channelId = req.headers['x-line-channel-id'] as string;
  if (!channelId) return res.status(400).json({ error: 'missing_channel_id' });

  try {
    const tenantId = await getTenantByChannelIdCached(channelId);
    req.tenantId = tenantId; // attach for downstream handlers
    // ... continue processing events
    res.json({ status: 'accepted' });
  } catch (err) {
    console.error(err);
    res.status(404).json({ error: 'tenant_not_found' });
  }
});
```

### 3.3 Default tenant seed (`server/src/db/seeds/01_default_tenant.sql`)

```sql
-- Idempotent seed: create default tenant + superadmin user if none exist
INSERT INTO tenant (id, name, line_channel_id, settings, created_at)
SELECT 'default-tenant', 'Default Tenant', 'default-channel-id', '{}', NOW()
WHERE NOT EXISTS (SELECT 1 FROM tenant LIMIT 1);

INSERT INTO "user" (id, tenant_id, email, name, role, password_hash, created_at)
SELECT 'superadmin-default', 'default-tenant', 'superadmin@workio.local', 'SuperAdmin', 'SuperAdmin', '$2b$10$fakehashforseedonly', NOW()
WHERE NOT EXISTS (SELECT 1 FROM "user" WHERE role = 'SuperAdmin' LIMIT 1);
```

Runner script (`server/src/db/seeds/seed.ts`):

```ts
import { execSync } from 'child_process';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

async function runSeed() {
  console.log('Seeding default tenant...');
  const sqlPath = join(__dirname, '01_default_tenant.sql');
  execSync(`psql ${process.env.DATABASE_URL} -f ${sqlPath}`, { stdio: 'inherit' });
  console.log('Seed complete.');
}

runSeed().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

### 3.4 Dev setup script (`scripts/setup-dev.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "🔧 Workio dev setup"

# .env
if [ ! -f server/.env ]; then
  echo "📄 Creating server/.env from example"
  cp server/.env.example server/.env
fi

# DB
if ! psql -lqt | cut -d \| -f 1 | grep -qw workio; then
  echo "🗄️  Creating database workio"
  createdb workio
fi

echo "📜 Running schema"
psql workio < server/src/db/schema.sql

echo "🌱 Seeding defaults"
cd server
DATABASE_URL="postgresql://localhost/workio" npm run seed 2>/dev/null || node -r esbuild-register src/db/seeds/seed.ts

echo "✅ Setup complete"
echo "Next steps:"
echo "  cd server && npm run dev"
echo "  cd .. && npm run dev"
```

Make executable:

```bash
chmod +x scripts/setup-dev.sh
```

Update package.json helper:

```json
"scripts": {
  "setup": "bash scripts/setup-dev.sh",
  "seed": "node -r esbuild-register src/db/seeds/seed.ts"
}
```

## 4. Verification

1. **Run setup** (idempotent):
   ```bash
   bash scripts/setup-dev.sh
   ```
   - Expect: database created, schema applied, seed inserts (no duplicate errors on re-run).

2. **Health/readiness**:
   ```bash
   curl http://localhost:3000/health
   # {"status":"ok","uptime":...}

   curl http://localhost:3000/ready
   # {"status":"ready","db":"up","lineConfig":"present"}  (when .env has LINE var)
   ```

3. **Tenant cache behavior**:
   - Start server, hit LINE webhook with a known `channelId` (via test route or curl with header).
   - First request: DB query logged.
   - Second request within 5m: no DB query for tenant resolution (verify via logs or by monitoring DB).
   - After 5m or manual `invalidateTenantCache`, next request fetches
