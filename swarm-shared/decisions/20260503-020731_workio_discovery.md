# workio / discovery

## Final Synthesis & Action Plan

Below is the single, consolidated answer that keeps only the strongest, non-overlapping insights and resolves contradictions in favor of correctness and concrete actionability.

---

## 1. Diagnosis (consolidated)

- **No automated discovery** of tenant-specific LINE webhook endpoints, secrets, or channel state across environments; onboarding requires manual edits and restarts, increasing misconfiguration risk.
- **Missing health/readiness checks** for LINE credentials and DB connectivity before/after config changes or deployments.
- **No tenant-level feature flags or capability matrix** (GPS, leave types, OT rules) — forces code or manual DB edits per tenant.
- **No tenant-scoped resilience** (rate-limit/circuit-breaker) for LINE API calls; one noisy tenant can exhaust tokens and block others.
- **No tenant-aware request/response correlation IDs** — cross-service debugging across webhook → queue → worker is slow and unreliable.
- **No discovery/registry** for available LINE OA channels and their webhook status (enabled/disabled, URL mismatch); ops must check LINE console manually.
- **No lightweight admin UI or CLI** to list tenants, LINE channel binding, and last webhook verification status — slows multi-tenant ops and debugging.

---

## 2. Proposed Change (single coherent scope)

Add a **discovery, health, and resilience module** that provides:

- Tenant discovery and health endpoints (`/api/discovery/tenants`, `/api/discovery/health/:tenantId?`).
- Tenant-scoped LINE channel introspection (cached) and webhook verification using tenant config.
- Tenant feature-flag resolution via a lightweight `tenant_feature_flags` table.
- Tenant-scoped rate-limiting and circuit-breaker for outbound LINE API calls.
- Tenant-aware correlation IDs on all webhook/queue/worker requests.
- Restricted access to discovery endpoints (SuperAdmin/Admin only).
- Optional thin admin UI or CLI surface on top of the same endpoints.

File-level scope:

- `server/src/routes/discovery.ts` (new)
- `server/src/services/discoveryService.ts` (new)
- `server/src/services/lineRateLimiter.ts` (new)
- `server/src/config/lineDiscovery.ts` (new)
- `server/src/middleware/discoveryAuth.ts` (new)
- `server/src/middleware/correlationId.ts` (new)
- `server/src/db/schema.sql` (append)

---

## 3. Implementation (corrected + actionable)

### 3.1 Schema (append to `server/src/db/schema.sql`)

```sql
-- Lightweight tenant feature flags
CREATE TABLE IF NOT EXISTS tenant_feature_flags (
  tenant_id   UUID    NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  flag_key    TEXT    NOT NULL,
  flag_value  TEXT    NOT NULL,
  updated_at  TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id, flag_key)
);

COMMENT ON TABLE tenant_feature_flags IS 'Per-tenant feature toggles (e.g., gps_required, leave_enabled, ot_enabled)';

-- Optional: tenant-level LINE resilience config
CREATE TABLE IF NOT EXISTS tenant_line_resilience (
  tenant_id          UUID    NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  max_rps            NUMERIC NOT NULL DEFAULT 1.0,
  circuit_breaker_failure_threshold INT NOT NULL DEFAULT 5,
  circuit_breaker_reset_seconds   INT NOT NULL DEFAULT 60,
  updated_at         TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id)
);
```

---

### 3.2 Tenant-aware correlation ID middleware (`server/src/middleware/correlationId.ts`)

```ts
import { Request, Response, NextFunction } from 'express';
import { v4 as uuidv4 } from 'uuid';

export function correlationId(req: Request, _res: Response, next: NextFunction) {
  const existing = req.headers['x-correlation-id'] as string || uuidv4();
  (req as any).correlationId = existing;
  next();
}

// Optional: attach to response and logging
export function attachCorrelationIdToLogger(req: Request, res: Response, next: NextFunction) {
  const cid = (req as any).correlationId;
  res.setHeader('X-Correlation-ID', cid);
  // If using a logger: logger.child({ correlationId: cid })
  next();
}
```

Wire into app early (before routes).

---

### 3.3 LINE discovery helper (`server/src/config/lineDiscovery.ts`)

```ts
import axios from 'axios';

export interface LineChannelInfo {
  channelId?: string;
  channelSecret: string;
  channelAccessToken: string;
  webhookUrl: string;
  webhookActive: boolean;
}

// LINE Channel Webhook Endpoint API
export async function getLineChannelInfo(
  channelAccessToken: string
): Promise<{ webhookUrl: string; webhookActive: boolean } | null> {
  try {
    const res = await axios.get('https://api.line.me/v2/bot/channel/webhook/endpoint', {
      headers: { Authorization: `Bearer ${channelAccessToken}` },
      timeout: 8000,
    });

    return {
      webhookUrl: res.data.endpoint || '',
      webhookActive: !!res.data.active,
    };
  } catch (err: any) {
    // 403/401 -> invalid token or missing scope; return null for clear handling
    return null;
  }
}
```

---

### 3.4 Tenant-scoped rate limiter + circuit breaker (`server/src/services/lineRateLimiter.ts`)

```ts
import Bottleneck from 'bottleneck';
import CircuitBreaker from 'opossum';

type LimiterMap = Map<string, Bottleneck>;
type BreakerMap = Map<string, CircuitBreaker>;

const limiters: LimiterMap = new Map();
const breakers: BreakerMap = new Map();

export function getLimiterForTenant(tenantId: string, rps = 1.0): Bottleneck {
  if (!limiters.has(tenantId)) {
    limiters.set(
      tenantId,
      new Bottleneck({
        minTime: rps > 0 ? 1000 / rps : 100,
        maxConcurrent: 2,
      })
    );
  }
  return limiters.get(tenantId)!;
}

export function getBreakerForTenant(
  tenantId: string,
  failureThreshold = 5,
  resetSeconds = 60
): CircuitBreaker {
  if (!breakers.has(tenantId)) {
    const breaker = new CircuitBreaker(
      (fn: () => Promise<any>) => fn(),
      {
        timeout: 15000,
        errorThresholdPercentage: 50,
        resetTimeout: resetSeconds * 1000,
        volumeThreshold: 3,
      }
    );

    breaker.fallback(() => ({ ok: false, error: 'LINE request unavailable (breaker open)' }));
    breaker.on('open', () => console.warn(`[breaker] open for tenant ${tenantId}`));
    breaker.on('halfOpen', () => console.info(`[breaker] half-open for tenant ${tenantId}`));
    breakers.set(tenantId, breaker);
  }
  return breakers.get(tenantId)!;
}
```

Use in LINE service calls:

```ts
const limiter = getLimiterForTenant(tenantId, tenantRps);
const breaker = getBreakerForTenant(tenantId, failureThreshold, resetSeconds);

const result = await breaker.fire(() =>
  limiter.schedule(() => axios.post(LINE_API_URL, payload, { headers }))
);
```

---

### 3.5 Discovery service (`server/src/services/discoveryService.ts`)

```ts
import { PoolClient } from 'pg';
import { getLineChannelInfo } from '../config/lineDiscovery';
import { listTenants, getTenantById } from './tenantService';
import { getTenantFeatures } from './featureFlagService';

export interface TenantDiscovery {
  id: string;
  name: string;
  lineChannelId?: string;
  lineWebhookUrl?: string;
  lineWebhookActive: boolean;
  features: Record<string, string>;
  dbOk: boolean;
}

export async function listTenantDiscoveries(client?: PoolClient): Promise<TenantDiscovery[]> {
  const tenants = await listTenants(client);
  const out: TenantDiscovery[] = [];

  for (const t of tenants) {
    const features = await getTenantFeatures(t.id
