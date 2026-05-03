# workio / discovery

## Final Synthesis — Best Parts, Resolved Contradictions, Concrete Action

**Core diagnosis (merged, de-duplicated):**
- No tenant-scoped ingestion pipeline for LINE → analytics warehouse; webhook events are ephemeral and mix across tenants, risking leakage and broken rollups.
- Clock events lack idempotency + monotonic ordering + temporal guard → duplicate/out-of-order rows from retries/double-taps.
- No discovery/metadata layer (channel → tenant, feature flags, schema versions) → onboarding/debugging require manual DB spelunking.
- Missing soft-delete + audit trail on tenant config changes → compliance/debugging opaque.
- No lightweight discovery CLI or knowledge-rag–style ops/docs hook → slow incident response and onboarding.

**Chosen scope (actionable, minimal-viable):**
1. Tenant-aware, idempotent, monotonic clock-event pipeline (webhook → DB) with temporal guard and schema migration.
2. Discovery CLI for tenant/channel health + last-N clock anomalies.
3. Soft-delete + audit table for tenant config changes (non-blocking, append-only).
4. Lightweight knowledge-rag hook (top-hub markdown + search) for ops/runbooks.

---

### 1) Schema changes (migration-first)

```sql
-- 1) Clock events: monotonic seq + idempotency + tenant isolation
ALTER TABLE clock_events
  ADD COLUMN IF NOT EXISTS seq            INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS client_dedupe_id TEXT,
  ADD COLUMN IF NOT EXISTS tenant_id     TEXT NOT NULL;  -- ensure tenant scoping exists

CREATE UNIQUE INDEX IF NOT EXISTS idx_clock_events_tenant_dedupe
  ON clock_events (tenant_id, client_dedupe_id)
  WHERE client_dedupe_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_clock_events_tenant_employee_seq
  ON clock_events (tenant_id, employee_id, seq DESC);

-- 2) Tenant config soft-delete + audit trail
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS deleted_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ DEFAULT now();

CREATE TABLE IF NOT EXISTS tenant_config_audit (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  changed_by    TEXT,
  operation     TEXT NOT NULL,   -- 'INSERT','UPDATE','DELETE'
  old_values    JSONB,
  new_values    JSONB,
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenant_config_audit_tenant
  ON tenant_config_audit (tenant_id, created_at DESC);

-- 3) LINE channel -> tenant mapping (discovery)
CREATE TABLE IF NOT EXISTS line_channels (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  channel_id    TEXT NOT NULL,
  feature_flags JSONB DEFAULT '{}',
  quota         JSONB,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now(),
  deleted_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_line_channels_tenant
  ON line_channels (tenant_id) WHERE deleted_at IS NULL;
```

---

### 2) Idempotent, monotonic clock service (single source of truth)

```ts
// workio/server/src/services/clockService.ts
import { db } from '../db';
import { clockEvents } from '../db/schema';
import { and, eq, desc, sql } from 'drizzle-orm';

export type ClockEventInput = {
  tenantId: string;
  employeeId: string;
  type: 'IN' | 'OUT';
  timestamp: Date;
  location?: { lat: number; lng: number };
  lineSource?: { userId: string; replyToken?: string };
  clientDedupeId?: string;
};

const SKEW_MS = 30_000;

export async function recordClockEvent(input: ClockEventInput) {
  const { tenantId, employeeId, type, timestamp, clientDedupeId } = input;

  // Idempotency: client-supplied nonce (rapid toggles / retries)
  if (clientDedupeId) {
    const dup = await db
      .select()
      .from(clockEvents)
      .where(
        and(
          eq(clockEvents.tenantId, tenantId),
          eq(clockEvents.clientDedupeId, clientDedupeId)
        )
      )
      .limit(1);
    if (dup.length > 0) return dup[0];
  }

  // Monotonic seq + temporal guard per tenant+employee
  const last = await db
    .select()
    .from(clockEvents)
    .where(
      and(eq(clockEvents.tenantId, tenantId), eq(clockEvents.employeeId, employeeId))
    )
    .orderBy(desc(clockEvents.seq))
    .limit(1);

  const nextSeq = last.length > 0 ? last[0].seq + 1 : 1;

  if (last.length > 0) {
    const lastTs = last[0].timestamp.getTime();
    const incomingTs = timestamp.getTime();
    // Allow small skew for retries; beyond skew, reject older events
    if (incomingTs + SKEW_MS < lastTs) {
      throw new Error('CLOCK_OUT_OF_ORDER');
    }
  }

  const [row] = await db
    .insert(clockEvents)
    .values({
      tenantId,
      employeeId,
      type,
      timestamp,
      location: input.location ? JSON.stringify(input.location) : null,
      lineUserId: input.lineSource?.userId ?? null,
      clientDedupeId: clientDedupeId ?? null,
      seq: nextSeq,
    })
    .returning();

  return row;
}
```

---

### 3) LINE webhook handler (tenant-scoped, idempotent)

```ts
// workio/server/src/routes/line.ts (excerpt)
import { recordClockEvent } from '../services/clockService';
import { lineChannels } from '../db/schema';
import { eq } from 'drizzle-orm';

async function resolveTenantByChannel(channelId: string): Promise<string | null> {
  const row = await db
    .select()
    .from(lineChannels)
    .where(and(eq(lineChannels.channelId, channelId), eq(lineChannels.deleted_at, null)))
    .limit(1);
  return row.length ? row[0].tenant_id : null;
}

export async function handleLineWebhook(body: any) {
  // Validate signature, resolve tenant
  const channelId = /* from LINE headers or body */;
  const tenantId = await resolveTenantByChannel(channelId);
  if (!tenantId) return;

  const employee = await resolveEmployeeByLineUserId(tenantId, body.source.userId);
  if (!employee) return;

  const type = /* infer IN/OUT from payload/action */;
  const clientDedupeId = body.clientDedupeId || generateClientDedupeId(body);

  await recordClockEvent({
    tenantId,
    employeeId: employee.id,
    type,
    timestamp: new Date(),
    location: body.location ? { lat: body.location.lat, lng: body.location.lng } : undefined,
    lineSource: { userId: body.source.userId, replyToken: body.events?.[0]?.replyToken },
    clientDedupeId,
  });
}

function generateClientDedupeId(body: any): string {
  const uid = body.events?.[0]?.source?.userId || 'unknown';
  const ts = Date.now();
  const rand = Math.random().toString(36).slice(2, 7);
  return `line:${uid}:${ts}:${rand}`;
}
```

---

### 4) Discovery CLI (tenant/channel health + anomalies)

```ts
// workio/scripts/discoverTenantHealth.ts
#!/usr/bin/env tsx
import { db } from '../server/src/db';
import { tenants, lineChannels, clockEvents } from '../server/src/db/schema';
import dayjs from 'dayjs';
import { eq, and, gte, desc } from 'drizzle-orm';

async function main() {
  const tenantsList = await db.select().from(tenants).where(eq(tenants.deleted_at, null));
  console.log('=== Tenant Health ===');
  for (const t of tenantsList) {
    const [channel] = await db
      .select()
      .from(lineChannels)
     
