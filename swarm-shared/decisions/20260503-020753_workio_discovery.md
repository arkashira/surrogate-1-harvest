# workio / discovery

## Final Consolidated Implementation Plan

Below merges the strongest, non-contradictory insights from both proposals into one correct, actionable plan.  
Where conflicts existed, choices favor **correctness, tenant isolation, and concrete DB-level guarantees**.

---

## 1. Diagnosis (merged and prioritized)

- **No tenant-scoped LINE channel binding** — webhook accepts any channel token; a misconfigured or shared token can cross-pollinate tenants and corrupt attendance records.
- **No automated discovery of LINE webhook contract drift** — backend ingests LINE events but has no contract test or schema snapshot to detect breaking changes from LINE (new event types, field removals, format changes).
- **Missing idempotency enforcement at DB level** — prior fixes added an idempotency table but rely on application logic; concurrent LINE retries can race before the unique index is hit.
- **No audit trail for clock-in/out mutations** — updates to `attendance` (punch, leave, OT) are silent; disputes cannot be traced to source event or user.
- **Missing tenant-level feature flags/discovery** — onboarding new tenants requires manual `.env` edits; no UI or API to discover/enable features (GPS, OT, leave types) per tenant.
- **No observability for LINE webhook health** — missing request/response logs, latency, and failure rates for `/webhook/line` makes incidents slow to detect.
- **Static role/permission matrix hard-coded** — roles and capabilities are implicit in route guards; no discoverable permission catalog or tenant overrides.
- **Environment/config drift risk** — `.env.example` exists but no validation or discovery endpoint to surface misconfigured required variables at runtime.

---

## 2. Proposed change (single coherent module)

Add a **discovery + safety module** that exposes:

- `GET /api/discovery/line-contract`  
  Returns normalized LINE webhook contract (cached, with last-updated) and validates incoming event shape against it (warn-only, non-breaking).

- `GET /api/discovery/tenant-features`  
  Returns tenant-scoped feature flags (GPS, OT, leave types) from `tenant_features`.

- `GET /api/discovery/permissions`  
  Returns role→permissions map (read-only) for UI/role discovery.

- `GET /api/health/line-webhook`  
  Returns recent webhook delivery stats (last 24h: count, success, duplicates, errors).

- **DB-level idempotency**  
  Enforce idempotency at the DB layer for LINE webhook events to prevent race conditions on retries.

- **Tenant-scoped LINE channel binding**  
  Bind each tenant to allowed LINE channel(s) and validate on webhook ingress.

- **Audit trail for attendance/leave/OT mutations**  
  Record immutable provenance for every punch/leave/OT change tied to source event and actor.

---

## 3. Implementation

### 3.1 Schema changes (`server/src/db/schema.sql`)

```sql
-- Tenant-scoped feature flags
CREATE TABLE IF NOT EXISTS tenant_features (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  feature_key VARCHAR(64) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT false,
  config JSONB DEFAULT '{}',
  UNIQUE(tenant_id, feature_key)
);

-- LINE contract snapshot (cached)
CREATE TABLE IF NOT EXISTS line_contract_cache (
  id SERIAL PRIMARY KEY,
  contract_type VARCHAR(32) NOT NULL DEFAULT 'webhook',
  version VARCHAR(16) NOT NULL,
  schema JSONB NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(contract_type)
);

-- Tenant-bound LINE channels (prevent cross-tenant token reuse)
CREATE TABLE IF NOT EXISTS tenant_line_channels (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  channel_id VARCHAR(64) NOT NULL,
  channel_secret VARCHAR(128) NOT NULL,
  channel_token VARCHAR(128) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, channel_id),
  UNIQUE(channel_id) -- one channel belongs to one tenant
);

-- DB-level idempotency for LINE webhook events
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key VARCHAR(128) PRIMARY KEY,
  tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
  event_type VARCHAR(64) NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash CHAR(64) NOT NULL
);

-- LINE webhook delivery audit (lightweight)
CREATE TABLE IF NOT EXISTS line_webhook_audit (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
  event_type VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL, -- 'ok', 'error', 'duplicate'
  line_signature VARCHAR(128),
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  processing_ms INTEGER,
  error_message TEXT
);

-- Immutable audit trail for attendance/leave/OT mutations
CREATE TABLE IF NOT EXISTS attendance_audit (
  id SERIAL PRIMARY KEY,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  attendance_id INTEGER NOT NULL,
  action VARCHAR(32) NOT NULL, -- 'punch', 'leave_request', 'leave_approve', 'ot_request', 'ot_approve', 'edit', 'delete'
  actor_type VARCHAR(32) NOT NULL, -- 'system', 'user', 'line_webhook'
  actor_id INTEGER, -- user.id or null for system/line
  line_event_id VARCHAR(128), -- when caused by LINE webhook
  before_state JSONB,
  after_state JSONB,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_line_webhook_audit_tenant ON line_webhook_audit(tenant_id, received_at);
CREATE INDEX IF NOT EXISTS idx_line_webhook_idempotency_key ON line_webhook_idempotency(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_attendance_audit_tenant ON attendance_audit(tenant_id, created_at);
```

---

### 3.2 Discovery service (`server/src/services/discoveryService.ts`)

```ts
import { Pool } from 'pg';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// --- Tenant features ---
export async function getTenantFeatures(tenantId: number) {
  const { rows } = await pool.query(
    'SELECT feature_key, enabled, config FROM tenant_features WHERE tenant_id = $1',
    [tenantId]
  );
  return rows;
}

// --- LINE contract snapshot ---
export async function upsertLineContract(version: string, schema: any) {
  await pool.query(
    `INSERT INTO line_contract_cache (contract_type, version, schema, fetched_at)
     VALUES ('webhook', $1, $2, NOW())
     ON CONFLICT (contract_type) DO UPDATE
     SET version = EXCLUDED.version, schema = EXCLUDED.schema, fetched_at = NOW()`,
    [version, JSON.stringify(schema)]
  );
}

export async function getCachedLineContract() {
  const { rows } = await pool.query(
    'SELECT version, schema, fetched_at FROM line_contract_cache WHERE contract_type = $1',
    ['webhook']
  );
  return rows[0] || null;
}

export async function fetchLineContractFromRemote() {
  // Minimal normalized LINE webhook contract (production: fetch from LINE sample endpoint or pinned schema)
  const normalized = {
    destination: 'string',
    events: [
      {
        type: 'string',
        mode: 'string',
        timestamp: 0,
        source: { type: 'string', userId: 'string', groupId: 'string', roomId: 'string' },
        message: { type: 'string', id: 'string', text: 'string' },
        postback: { data: 'string' },
        beacon: { hwid: 'string', type: 'string', dm: 'string' }
      }
    ]
  };
  await upsertLineContract('v2.5', normalized);
  return normalized;
}

// --- LINE webhook audit ---
export async function recordWebhookAudit(
  tenantId: number | null,
  eventType: string,
  status: '
