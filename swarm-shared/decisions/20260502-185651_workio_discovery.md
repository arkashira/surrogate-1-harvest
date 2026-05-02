# workio / discovery

## 1. Diagnosis

- **No tenant resolution on LINE webhook** — every webhook request must query DB by `channelId` to find tenant; slow, noisy, and fails fast under load if DB is cold.
- **Missing health/readiness endpoints** — no `/health` or `/ready` for k8s/PM2/liveness checks; deployment and rollback are blind.
- **No idempotent seed for tenant schema** — onboarding new tenants relies on runtime migrations that can partially apply; breaks CI/CD and multi-tenant correctness.
- **LINE signature validation is best-effort** — missing constant-time signature check and replay-window; allows spoofed events and clock-skew replays.
- **No structured logging/request-id** — debugging across webhook → worker → DB is needle-in-haystack; slows incident resolution.

## 2. Proposed change

Add a **lightweight tenant cache + readiness probe + idempotent tenant seed** with minimal surface:

- Files:  
  - `server/src/webhook/line.ts` (add signature validation + tenant cache)  
  - `server/src/routes/health.ts` (new)  
  - `server/src/db/seed/tenant.sql` (new)  
  - `server/src/index.ts` (mount routes + request-id middleware)

## 3. Implementation

```bash
# 1) Add health/readiness endpoint
cat > server/src/routes/health.ts <<'EOF'
import { Router } from 'express';
import { db } from '../db';

const router = Router();

router.get('/health', (_req, res) => res.json({ status: 'ok' }));

router.get('/ready', async (_req, res) => {
  try {
    await db.query('SELECT 1');
    res.json({ status: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'unavailable', error: String(err) });
  }
});

export default router;
EOF
```

```bash
# 2) Idempotent tenant seed (run once per tenant)
cat > server/src/db/seed/tenant.sql <<'EOF'
-- Idempotent: safe to re-run
INSERT INTO tenants (name, line_channel_id, line_channel_secret, line_access_token, timezone, created_at, updated_at)
VALUES
  ('Workio Demo', '{{LINE_CHANNEL_ID}}', '{{LINE_CHANNEL_SECRET}}', '{{LINE_ACCESS_TOKEN}}', 'Asia/Bangkok', NOW(), NOW())
ON CONFLICT (line_channel_id) DO UPDATE
  SET name = EXCLUDED.name,
      line_channel_secret = EXCLUDED.line_channel_secret,
      line_access_token = EXCLUDED.line_access_token,
      timezone = EXCLUDED.timezone,
      updated_at = NOW();
EOF
```

```typescript
// 3) server/src/webhook/line.ts — add crypto validation + tenant cache
import crypto from 'crypto';
import { db } from '../db';

const TENANT_TTL_MS = 5 * 60 * 1000; // 5m
const tenantCache = new Map<string, { tenant: any; ts: number }>();

function constantTimeEqual(a: string, b: string): boolean {
  return crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
}

export function verifyLineSignature(rawBody: string, signature: string, secret: string): boolean {
  const expected = crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
  return constantTimeEqual(signature, expected);
}

export async function resolveTenantByChannelId(channelId: string) {
  const cached = tenantCache.get(channelId);
  if (cached && Date.now() - cached.ts < TENANT_TTL_MS) return cached.tenant;

  const { rows } = await db.query('SELECT * FROM tenants WHERE line_channel_id = $1 LIMIT 1', [channelId]);
  const tenant = rows[0] || null;
  if (tenant) tenantCache.set(channelId, { tenant, ts: Date.now() });
  return tenant;
}
```

```typescript
// 4) server/src/index.ts — mount + request-id
import express from 'express';
import helmet from 'helmet';
import morgan from 'morgan';
import { v4 as uuidv4 } from 'uuid';
import healthRoute from './routes/health';
import lineWebhookRoute from './webhook/line';

const app = express();
app.use(helmet());
app.use(express.json({ limit: '1mb', verify: (req, res, buf) => ((req as any).rawBody = buf.toString()) }));

// request-id middleware
app.use((req, res, next) => {
  (req as any).requestId = req.headers['x-request-id'] || uuidv4();
  res.setHeader('X-Request-Id', (req as any).requestId);
  next();
});

app.use(morgan('combined'));
app.use('/health', healthRoute);
app.use('/webhook/line', lineWebhookRoute);

export default app;
```

```typescript
// 5) server/src/webhook/line.ts — handler update (excerpt)
import { verifyLineSignature, resolveTenantByChannelId } from './line';

router.post('/line', async (req, res) => {
  const rawBody = (req as any).rawBody;
  const signature = req.headers['x-line-signature'] as string;
  // Fetch first tenant secret for initial validation; refine after cache lookup if needed
  const { rows } = await db.query('SELECT line_channel_secret FROM tenants WHERE line_channel_id = $1 LIMIT 1', [req.body.destination || '']);
  const secret = rows[0]?.line_channel_secret;

  if (!secret || !verifyLineSignature(rawBody, signature, secret)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const tenant = await resolveTenantByChannelId(req.body.destination);
  if (!tenant) return res.status(404).json({ error: 'Tenant not found' });

  // ... process events
  res.json({});
});
```

## 4. Verification

1. **Seed tenant**  
   ```bash
   psql workio < server/src/db/seed/tenant.sql
   ```
   Re-run to confirm idempotency (no duplicate rows).

2. **Health/readiness**  
   ```bash
   curl http://localhost:3000/health
   # {"status":"ok"}
   curl -v http://localhost:3000/ready
   # HTTP/1.1 200 {"status":"ready"}  (or 503 if DB down)
   ```

3. **LINE webhook signature**  
   - Use `ngrok` to expose `/webhook/line`.
   - Send a test event from LINE console with correct/incorrect `x-line-signature`; expect 200 for valid, 401 for invalid.

4. **Tenant cache**  
   - Hit webhook twice with same `destination` within 5m; second request should skip DB query (verify via logs or `EXPLAIN ANALYZE`).

5. **Request-ID propagation**  
   - `curl -H "X-Request-Id: test-123" http://localhost:3000/health` → response header `X-Request-Id: test-123`.
