# workio / discovery

## Final Synthesized Implementation Plan  
*(merged strongest parts, resolved contradictions, maximized correctness + actionability)*

---

### 1. Diagnosis — consolidated critical gaps
- **Tenant onboarding missing**: no self-serve tenant creation or first-admin bootstrap.  
- **LINE webhook insecure**: no signature verification, no idempotency, no structured observability.  
- **Clock state missing guardrails**: no state machine to prevent double clock-in, enforce max session length, or require clock-out.  
- **Location validation absent**: GPS accepted without schema/bounds/accuracy checks; no enforcement that clock events include valid location.  
- **Observability & tenant routing weak**: no request-scoped logging, no tenant-aware caching, no health/readiness per tenant.

---

### 2. Proposed change (scope)
- Add **tenant onboarding flow** (self-serve + first-admin bootstrap).  
- Add **secure, idempotent LINE webhook** with signature verification, structured logging, and tenant-aware routing.  
- Add **clock state machine** (guardrails: no double clock-in, max session length, require clock-out).  
- Add **location validation and enforcement** for clock events.  
- Add **per-tenant health/readiness probe** and observability.

Files touched/created:
- `workio/server/src/routes/tenant-onboarding.ts`
- `workio/server/src/routes/line-webhook.ts`
- `workio/server/src/middleware/` (logger, signature, idempotency, location validation)
- `workio/server/src/services/tenant.ts`
- `workio/server/src/services/clock.ts`
- `workio/server/src/services/line.ts`
- `workio/server/src/lib/state-machine.ts`
- `workio/server/src/lib/cache.ts`
- `workio/server/src/lib/logger.ts`
- Wire into `app.ts` and add health route.

---

### 3. Implementation (production-ready minimal surface)

#### 3.1 Logger (`workio/server/src/lib/logger.ts`)
```ts
import pino from 'pino';

const transport = pino.transport
  ? pino.transport({ target: 'pino-pretty', options: { colorize: true } })
  : undefined;

export const logger = pino(
  {
    level: process.env.LOG_LEVEL || 'info',
    timestamp: pino.stdTimeFunctions.isoTime,
    redact: ['req.headers.authorization', 'req.headers.cookie'],
  },
  transport
);
```

#### 3.2 Cache (`workio/server/src/lib/cache.ts`)
```ts
import NodeCache from 'node-cache';
const cache = new NodeCache({ stdTTL: 300, checkperiod: 60 });

export { cache };
```

#### 3.3 Tenant service with onboarding (`workio/server/src/services/tenant.ts`)
```ts
import { cache } from '../lib/cache';
import { db } from '../db'; // adapt to your ORM

export const tenantService = {
  async createTenant(name: string, adminEmail: string, lineChannelId?: string) {
    // idempotent by adminEmail or name+lineChannelId depending on business rule
    const existing = await db.tenants.findOne({ adminEmail });
    if (existing) return existing;

    const tenant = await db.tenants.insert({
      name,
      adminEmail,
      lineChannelId,
      createdAt: new Date(),
    });
    return tenant;
  },

  async resolveByLineUserId(lineUserId: string) {
    const key = `tenant:lineUser:${lineUserId}`;
    let tenant = cache.get(key);
    if (tenant) return tenant;

    tenant = await db.tenants.findOne({ lineUserIds: { $in: [lineUserId] } });
    if (tenant) cache.set(key, tenant, 300);
    return tenant;
  },

  async getTenantHealth(tenantId: string) {
    const checks = {
      db: false,
      lineConfig: false,
    };
    try {
      await db.tenants.findOne({ id: tenantId });
      checks.db = true;
    } catch {}
    const tenant = await db.tenants.findOne({ id: tenantId });
    checks.lineConfig = Boolean(tenant?.lineChannelId && process.env.LINE_CHANNEL_SECRET);
    return { tenantId, ok: checks.db && checks.lineConfig, checks };
  },
};
```

#### 3.4 Clock state machine (`workio/server/src/lib/state-machine.ts`)
```ts
import { db } from '../db';

const MAX_SESSION_HOURS = 12;

export const clockStateMachine = {
  async canClockIn(userId: string, tenantId: string) {
    const last = await db.clockEvents.findOne(
      { userId, tenantId },
      { sort: { timestamp: -1 } }
    );
    if (!last) return { ok: true, reason: 'first clock-in' };
    if (last.type === 'out') return { ok: true, reason: 'previous clock-out' };
    // last is 'in' -> prevent double clock-in
    const ageHours = (Date.now() - new Date(last.timestamp).getTime()) / (1000 * 60 * 60);
    if (ageHours >= MAX_SESSION_HOURS) {
      // force clock-out for safety and allow new clock-in
      await db.clockEvents.insert({
        userId,
        tenantId,
        type: 'out',
        timestamp: new Date(),
        forced: true,
        note: 'max session exceeded',
      });
      return { ok: true, reason: 'forced clock-out for max session, now allowed to clock-in' };
    }
    return { ok: false, reason: 'already clocked in' };
  },

  async canClockOut(userId: string, tenantId: string) {
    const last = await db.clockEvents.findOne(
      { userId, tenantId },
      { sort: { timestamp: -1 } }
    );
    if (!last) return { ok: false, reason: 'no prior clock-in' };
    if (last.type === 'out') return { ok: false, reason: 'already clocked out' };
    return { ok: true, reason: 'can clock-out' };
  },
};
```

#### 3.5 Clock service (`workio/server/src/services/clock.ts`)
```ts
import { db } from '../db';
import { clockStateMachine } from '../lib/state-machine';
import { validateLocation } from '../middleware/validateLocation';
import { Request } from 'express';

export const clockService = {
  async handleClockRequest(
    userId: string,
    intent: 'in' | 'out',
    ctx: { tenant: any; requestId: string; log: any },
    location?: { lat: number; lon: number; accuracy?: number }
  ) {
    const { tenant, log } = ctx;
    const can =
      intent === 'in'
        ? await clockStateMachine.canClockIn(userId, tenant.id)
        : await clockStateMachine.canClockOut(userId, tenant.id);

    if (!can.ok) {
      log.warn({ msg: 'clock-guard-rejected', intent, reason: can.reason });
      return { ok: false, reason: can.reason };
    }

    // If location required, enforce presence and validity
    if (process.env.ENFORCE_LOCATION === 'true') {
      if (!location) {
        log.warn({ msg: 'location-required-missing', intent });
        return { ok: false, reason: 'location required' };
      }
      // quick runtime validation (server-side guard)
      if (
        !Number.isFinite(location.lat) ||
        location.lat < -90 ||
        location.lat > 90 ||
        !Number.isFinite(location.lon) ||
        location.lon < -180 ||
        location.lon > 180 ||
        (location.accuracy != null && location.accuracy < 0)
      ) {
        log.warn({ msg: 'location-invalid', location });
        return { ok: false, reason: 'location invalid' };
      }
    }

    const record = await db.clockEvents.insert({
      userId,
      tenantId: tenant.id,
      type: intent,
      timestamp: new Date(),
      location,
    });

    log.info({ msg: 'clock-event-recorded', intent, recordId: record.id });
    return { ok: true, record };
  },

