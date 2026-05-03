# workio / discovery

## Final Synthesis (merged, de-duplicated, contradiction-resolved)

**Guiding rules applied**
- Correctness first: idempotency + tenant isolation + observability are non-negotiable.
- Concrete actionability: every item includes schema, code, or config change you can implement now.
- Contradictions resolved:
  - Observability: merge Candidate 1’s structured logging/correlation IDs with Candidate 2’s tenant-scoped health/metrics into one coherent surface.
  - Multi-tenancy: keep Candidate 2’s tenant context (URL + header routing) and embed Candidate 1’s idempotency/audit at the tenant level.
  - Circuit-breaker/retry: keep Candidate 1’s outbound LINE client protections; extend them to be tenant-scoped (token/channel isolation).
  - Discovery/health: unify into a single `/health` (liveness/readiness) + `/admin/tenants` (tenant discovery/telemetry) surface.

---

## 1. Diagnosis (merged)

- No observability into LINE webhook health (missing request/response logs, no alerting on failures/retries).
- No idempotency enforcement at the API boundary (LINE retries can create duplicate punches).
- No circuit-breaker/retry budget for outbound LINE API calls (transient outages cascade).
- No tenant-scoped discovery/telemetry (ops cannot list tenants, channel status, last punch, active employees).
- No tenant context in LINE webhook routing (hardcoded `/webhook/line`; no isolation).
- No tenant-scoped feature flags to toggle LINE/webhook/clock-in/GPS/leave features without redeploy.
- No lightweight discovery endpoint for automated probing/rollout checks.

---

## 2. Proposed change (merged scope)

- Add atomic, idempotent, tenant-isolated LINE webhook handler with structured logging, correlation IDs, and tenant-scoped audit.
- Add tenant model + LINE channel config + feature flags (editable by SuperAdmin).
- Add tenant-scoped circuit-breaker/retry for outbound LINE notify.
- Add discovery/health endpoints:
  - `GET /health` (liveness/readiness + DB + LINE channel basic reachability per tenant).
  - `GET /admin/tenants` (tenant discovery/telemetry for ops/dashboard).
- Add middleware: correlation ID, structured logger, tenant resolver.

Files to change/add:
- `server/src/db/schema.sql` (tenants, tenant_line_channels, tenant_feature_flags, line_webhook_events).
- `server/src/middleware/` (correlationId.ts, logger.ts, tenantResolver.ts).
- `server/src/routes/line.ts` (tenant-aware webhook).
- `server/src/routes/health.ts` (health + tenant checks).
- `server/src/routes/admin/tenants.ts` (discovery/telemetry).
- `server/src/lib/line/client.ts` (tenant-scoped circuit-breaker + retry).
- `server/src/lib/tenant/flags.ts` (feature flag helpers).

---

## 3. Implementation (merged, production-ready)

### 3.1 Schema (tenant + idempotency + audit)

```sql
-- server/src/db/schema.sql

-- Tenants
CREATE TABLE IF NOT EXISTS tenants (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- LINE channel per tenant (supports multiple channels per tenant if needed)
CREATE TABLE IF NOT EXISTS tenant_line_channels (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  channel_name    TEXT NOT NULL,
  access_token    TEXT NOT NULL,
  channel_secret  TEXT,
  webhook_url     TEXT,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  last_checked_at TIMESTAMPTZ,
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, channel_name)
);

-- Tenant-scoped feature flags
CREATE TABLE IF NOT EXISTS tenant_feature_flags (
  tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  flag_key        TEXT NOT NULL,
  flag_value      BOOLEAN NOT NULL DEFAULT false,
  updated_by      TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, flag_key)
);

-- LINE webhook events (idempotency + audit) - tenant-scoped
CREATE TABLE IF NOT EXISTS line_webhook_events (
  tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_line_webhook_events_tenant_created
  ON line_webhook_events(tenant_id, created_at);
```

---

### 3.2 Middleware

```ts
// server/src/middleware/logger.ts
import pino from 'pino';
import { Request, Response, NextFunction } from 'express';

export const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  transport: process.env.NODE_ENV !== 'production' ? { target: 'pino-pretty' } : undefined,
});

export function requestLogger(req: Request, res: Response, next: NextFunction) {
  const start = Date.now();
  res.on('finish', () => {
    logger.info({
      method: req.method,
      url: req.originalUrl,
      status: res.statusCode,
      durationMs: Date.now() - start,
      correlationId: req.headers['x-correlation-id'],
      tenantId: (req as any).tenantId,
    }, 'request');
  });
  next();
}
```

```ts
// server/src/middleware/correlationId.ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';

export function correlationId(req: Request, _res: Response, next: NextFunction) {
  const id = (req.headers['x-correlation-id'] as string) || crypto.randomUUID();
  req.headers['x-correlation-id'] = id;
  (req as any).correlationId = id;
  next();
}
```

```ts
// server/src/middleware/tenantResolver.ts
import { Request, Response, NextFunction } from 'express';
import { db } from '../db/index.js';

// Supports:
// - x-tenant-id header
// - subdomain (tenant-id.localhost)
// - /:tenantId/... route prefix (mount routers under /:tenantId)
export async function tenantResolver(req: Request, res: Response, next: NextFunction) {
  let tenantId = req.headers['x-tenant-id'] as string;

  if (!tenantId && req.subdomains?.length) {
    tenantId = req.subdomains[0];
  }

  if (!tenantId && req.params?.tenantId) {
    tenantId = req.params.tenantId;
  }

  if (!tenantId) {
    return res.status(400).json({ error: 'missing tenant context' });
  }

  const tenant = await db`SELECT * FROM tenants WHERE id = ${tenantId}`.then((r) => r[0]);
  if (!tenant || !tenant.is_active) {
    return res.status(404).json({ error: 'tenant not found or inactive' });
  }

  (req as any).tenantId = tenantId;
  (req as any).tenant = tenant;
  next();
}
```

---

### 3.3 Tenant-scoped feature flag helper

```ts
// server/src/lib/tenant/flags.ts
import { db } from '../db/index.js';

export async function isFeatureEnabled(tenantId: string, flagKey: string): Promise<boolean> {
  const row = await db`
    SELECT flag_value FROM tenant_feature_flags
    WHERE tenant_id = ${tenantId} AND flag_key = ${flagKey}
  `.then((r) => r[0]);
  return row?.flag_value === true;
}

export async function setFeatureFlag(tenantId: string, flagKey: string, value: boolean, updatedBy?: string) {
  await db`
    INSERT INTO tenant_feature
