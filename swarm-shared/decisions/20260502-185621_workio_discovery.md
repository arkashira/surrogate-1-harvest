# workio / discovery

## Final Synthesis (Correctness + Actionability)

Merge the strongest, non-contradictory insights into one coherent plan.  
Prioritize:  
1) Correctness (security, data integrity)  
2) Concrete, small-scope deliverables (≤2–3 days)  
3) Multi-tenant correctness from day one (tenant-scoped secrets, safe defaults)

---

## 1. Diagnosis (merged, de-duplicated)

- **No tenant onboarding/self-serve bootstrap**: No flow to create tenant + first admin + default roles/permissions, so empty tenants fail on first use.
- **Global LINE secrets (not tenant-scoped)**: `.env`-level LINE config prevents multiple LINE OA instances in multi-tenant deployments.
- **Missing LINE webhook validation & signature verification**: No `X-Line-Signature` check; endpoint is open to spoofed events and replay.
- **No clock-in/out state machine or idempotency**: Employees can double-clock or enter invalid states, corrupting attendance records.
- **No location verification enforcement**: GPS feature is declared but unimplemented on the backend.
- **No health/readiness endpoint**: Orchestration and uptime checks cannot verify tenant state or LINE connectivity.
- **Missing seed defaults**: No default roles/permissions or tenant bootstrap rows, causing first-login errors.

---

## 2. Proposed change (single coherent plan)

Implement in three small, reviewable slices (all required; ordered by dependency):

### Slice A — Tenant bootstrap + safe defaults (1 day)
- Add idempotent seed roles/permissions to `schema.sql`.
- Add `POST /tenants` (create tenant) and `POST /tenants/:id/onboard` (bootstrap tenant + first admin) with tenant-scoped LINE config fields.
- Persist LINE secrets per-tenant (`line_channel_access_token`, `line_channel_secret`, `line_webhook_url`) and mark them nullable (global `.env` fallback only for single-tenant legacy mode).

### Slice B — Health/readiness + LINE smoke tests (0.5 day)
- Add `/health` (liveness) and `/ready` (readiness) endpoints.
- Readiness checks: DB connectivity, and (if tenant-scoped or global LINE config present) lightweight LINE smoke tests (`/v2/bot/info`), plus webhook URL reachability.
- Return 200/503 with component-level statuses for orchestration.

### Slice C — Security + attendance correctness (1–1.5 days)
- Add LINE webhook signature verification middleware (`validateLineSignature`) using `X-Line-Signature`, constant-time compare, and short replay window (timestamp ±5 min, nonce cache).
- Implement clock-in/out state machine + idempotency:
  - One open `attendance_session` per employee per tenant (enforced by DB unique constraint).
  - Valid transitions: `clock_in` (no open session) → `clock_out`/`break_start`/`break_end`; reject invalid transitions with 409.
  - Idempotency key support for client retries.
- Add location verification enforcement (opt-in per tenant):
  - On clock-in/out, validate `latitude`/`longitude` against allowed geofences (`tenant_geofences` table).
  - If enabled and no geofence match, reject with 400.

---

## 3. Implementation (merged best parts, corrected)

### 3.1 Schema additions/updates (`server/src/db/schema.sql`)

```sql
-- Tenants (if not exists)
CREATE TABLE IF NOT EXISTS tenants (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  line_channel_access_token TEXT,
  line_channel_secret      TEXT,
  line_webhook_url         TEXT,
  location_verification_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Default roles (idempotent)
INSERT INTO roles (name, description, is_default)
VALUES
  ('SuperAdmin', 'Full system access across tenants', true),
  ('Admin',      'Manage own tenant, employees, settings', true),
  ('Manager',    'Approve leave/OT, view reports', true),
  ('Employee',   'Clock in/out, request leave/OT', true)
ON CONFLICT (name) DO NOTHING;

-- Default permissions (idempotent)
INSERT INTO permissions (name, description)
VALUES
  ('manage_tenant',    'Manage tenant settings and users'),
  ('manage_roles',     'Manage roles and permissions'),
  ('manage_attendance','Manage clock in/out and attendance'),
  ('manage_leave_ot',  'Approve leave and OT requests'),
  ('view_reports',     'View dashboards and reports')
ON CONFLICT (name) DO NOTHING;

-- Role->permissions mapping (idempotent)
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE
  (r.name = 'SuperAdmin') OR
  (r.name = 'Admin'     AND p.name IN ('manage_tenant','manage_roles','manage_attendance','manage_leave_ot','view_reports')) OR
  (r.name = 'Manager'   AND p.name IN ('manage_attendance','manage_leave_ot','view_reports')) OR
  (r.name = 'Employee'  AND p.name IN ('manage_attendance','manage_leave_ot'))
ON CONFLICT DO NOTHING;

-- Attendance session (one open session per employee per tenant)
CREATE TABLE IF NOT EXISTS attendance_sessions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  employee_id     UUID NOT NULL, -- references users(id)
  clock_in_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  clock_out_at    TIMESTAMPTZ,
  last_status     TEXT NOT NULL CHECK (last_status IN ('clocked_in','break_started','break_ended')),
  location_lat    DOUBLE PRECISION,
  location_lon    DOUBLE PRECISION,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, employee_id) WHERE (clock_out_at IS NULL)
);

-- Geofences per tenant
CREATE TABLE IF NOT EXISTS tenant_geofences (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  center_lat  DOUBLE PRECISION NOT NULL,
  center_lon  DOUBLE PRECISION NOT NULL,
  radius_m    DOUBLE PRECISION NOT NULL CHECK (radius_m > 0),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

### 3.2 Tenant service (`server/src/services/tenantService.ts`)

```ts
import { Pool, QueryResult } from 'pg';
import axios from 'axios';
import crypto from 'crypto';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

export interface TenantConfig {
  name: string;
  lineChannelAccessToken?: string;
  lineChannelSecret?: string;
  lineWebhookUrl?: string;
  adminEmail: string;
  adminPassword: string;
  locationVerificationEnabled?: boolean;
}

/* Create tenant + first admin in tx */
export async function createTenant(config: TenantConfig) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const tenantRes = await client.query(
      `INSERT INTO tenants (name, line_channel_access_token, line_channel_secret, line_webhook_url, location_verification_enabled)
       VALUES ($1,$2,$3,$4,$5) RETURNING id`,
      [config.name, config.lineChannelAccessToken, config.lineChannelSecret, config.lineWebhookUrl, Boolean(config.locationVerificationEnabled)]
    );
    const tenantId = tenantRes.rows[0].id;

    const adminRoleRes = await client.query(`SELECT id FROM roles WHERE name = 'Admin' LIMIT 1`);
    if (!adminRoleRes.rows.length) throw new Error('Admin role missing');

    const userRes = await client.query(
      `INSERT INTO users (tenant_id, email, password_hash, role_id)
       VALUES ($1,$2,crypt($3, gen_salt('bf')),$4) RETURNING id`,
      [tenantId, config.adminEmail, config.adminPassword, adminRoleRes.rows[0].id]
    );

    await client.query('COMMIT');
    return { tenantId, userId: userRes.rows[0].id };
  } catch (e) {
    await client.query('ROLLBACK');
    throw e;
  } finally {
    client.release();

