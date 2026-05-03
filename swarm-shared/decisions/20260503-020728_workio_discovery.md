# workio / discovery

## Final synthesized implementation plan

**Core principle**: Add a minimal, role-gated tenant discovery + LINE provisioning surface that is **per-tenant secure**, **operationally observable**, and **immediately actionable** for SuperAdmin/Admin.

---

### 1. Schema (run once)

```sql
-- Tenant-scoped LINE configuration (isolated secrets, replay-safe)
CREATE TABLE IF NOT EXISTS tenant_line_config (
  tenant_id      UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  channel_secret TEXT NOT NULL,
  channel_token  TEXT,
  webhook_url    TEXT,
  verified_at    TIMESTAMPTZ,
  updated_at     TIMESTAMPTZ DEFAULT now()
);

-- Tenant feature flags and limits (actionable policy controls)
CREATE TABLE IF NOT EXISTS tenant_features (
  tenant_id        UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  gps_required     BOOLEAN NOT NULL DEFAULT true,
  ot_enabled       BOOLEAN NOT NULL DEFAULT true,
  leave_enabled    BOOLEAN NOT NULL DEFAULT true,
  punch_window     INT NOT NULL DEFAULT 30,      -- minutes before/after shift
  max_daily_hours  INT NOT NULL DEFAULT 12,
  updated_at       TIMESTAMPTZ DEFAULT now()
);

-- Read-optimized recent activity projection (debug/admin UX)
CREATE TABLE IF NOT EXISTS tenant_recent_activity (
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL,
  event_type  TEXT NOT NULL,              -- 'punch', 'leave_request', 'ot_request', etc.
  event_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload     JSONB,
  PRIMARY KEY (tenant_id, event_at, user_id, event_type)
);

-- Indexes for fast admin queries
CREATE INDEX IF NOT EXISTS idx_tenant_recent_activity_tenant ON tenant_recent_activity (tenant_id, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_tenant_recent_activity_user ON tenant_recent_activity (tenant_id, user_id, event_at DESC);
```

---

### 2. Tenant discovery + LINE provisioning route (`server/src/routes/tenants.ts`)

```ts
import { Router } from 'express';
import { pool } from '../db';
import { verifyLineWebhook } from '../services/line.service';
import { requireRole } from '../middleware/auth';

const router = Router();

// List tenants (SuperAdmin) or own tenant (Admin/Manager/Employee)
router.get('/', requireRole(['SuperAdmin', 'Admin', 'Manager', 'Employee']), async (req, res) => {
  const user = req.user!;
  let rows;
  if (user.role === 'SuperAdmin') {
    rows = await pool.query(`
      SELECT t.id, t.name, t.status, t.created_at,
             lc.channel_secret IS NOT NULL AS line_linked,
             lc.verified_at,
             tf.gps_required, tf.ot_enabled, tf.leave_enabled
      FROM tenants t
      LEFT JOIN tenant_line_config lc ON lc.tenant_id = t.id
      LEFT JOIN tenant_features tf ON tf.tenant_id = t.id
    `);
  } else {
    rows = await pool.query(`
      SELECT t.id, t.name, t.status,
             lc.channel_secret IS NOT NULL AS line_linked,
             lc.verified_at,
             tf.gps_required, tf.ot_enabled, tf.leave_enabled,
             tf.punch_window, tf.max_daily_hours
      FROM tenants t
      LEFT JOIN tenant_line_config lc ON lc.tenant_id = t.id
      LEFT JOIN tenant_features tf ON tf.tenant_id = t.id
      WHERE t.id = $1
    `, [user.tenantId]);
  }
  res.json(rows.rows);
});

// Single tenant detail + recent activity (role-gated)
router.get('/:id', requireRole(['SuperAdmin', 'Admin', 'Manager', 'Employee']), async (req, res) => {
  const user = req.user!;
  const { id } = req.params;
  if (user.role !== 'SuperAdmin' && user.tenantId !== id) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  const [tenant, line, features, activity] = await Promise.all([
    pool.query('SELECT id, name, status, created_at FROM tenants WHERE id = $1', [id]),
    pool.query(
      'SELECT channel_secret IS NOT NULL AS linked, verified_at, webhook_url FROM tenant_line_config WHERE tenant_id = $1',
      [id]
    ),
    pool.query('SELECT * FROM tenant_features WHERE tenant_id = $1', [id]),
    pool.query(
      `SELECT user_id, event_type, event_at, payload
       FROM tenant_recent_activity
       WHERE tenant_id = $1
       ORDER BY event_at DESC
       LIMIT 50`,
      [id]
    ),
  ]);

  if (tenant.rows.length === 0) return res.status(404).json({ error: 'Tenant not found' });
  res.json({
    ...tenant.rows[0],
    line: line.rows[0] || null,
    features: features.rows[0] || null,
    recent_activity: activity.rows,
  });
});

// Verify LINE webhook for tenant (Admin+)
router.post('/:id/line/verify', requireRole(['SuperAdmin', 'Admin']), async (req, res) => {
  const { id } = req.params;
  const { channelSecret, webhookUrl } = req.body;
  if (!channelSecret) return res.status(400).json({ error: 'channelSecret required' });

  try {
    const ok = await verifyLineWebhook(channelSecret, webhookUrl);
    if (ok) {
      await pool.query(
        `INSERT INTO tenant_line_config (tenant_id, channel_secret, webhook_url, verified_at)
         VALUES ($1, $2, $3, now())
         ON CONFLICT (tenant_id) DO UPDATE
         SET channel_secret = EXCLUDED.channel_secret,
             webhook_url = EXCLUDED.webhook_url,
             verified_at = now(),
             updated_at = now()`,
        [id, channelSecret, webhookUrl || `https://${process.env.DOMAIN}/webhook/line`]
      );
    }
    res.json({ verified: ok });
  } catch (err) {
    res.status(400).json({ verified: false, error: String(err) });
  }
});

// Upsert tenant features (Admin+)
router.put('/:id/features', requireRole(['SuperAdmin', 'Admin']), async (req, res) => {
  const { id } = req.params;
  const { gps_required, ot_enabled, leave_enabled, punch_window, max_daily_hours } = req.body;
  await pool.query(
    `INSERT INTO tenant_features (tenant_id, gps_required, ot_enabled, leave_enabled, punch_window, max_daily_hours)
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT (tenant_id) DO UPDATE
     SET gps_required = EXCLUDED.gps_required,
         ot_enabled = EXCLUDED.ot_enabled,
         leave_enabled = EXCLUDED.leave_enabled,
         punch_window = EXCLUDED.punch_window,
         max_daily_hours = EXCLUDED.max_daily_hours,
         updated_at = now()`,
    [id, gps_required, ot_enabled, leave_enabled, punch_window, max_daily_hours]
  );
  res.json({ ok: true });
});

export default router;
```

---

### 3. LINE service helper (`server/src/services/line.service.ts`)

```ts
import axios from 'axios';

async function getChannelToken(channelSecret: string): Promise<string> {
  const resp = await axios.post(
    'https://api.line.me/oauth2/v2.1/token',
    'grant_type=client_credentials&client_id=channelId&client_secret=' + encodeURIComponent(channelSecret),
    {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      timeout: 8000,
    }
  );
  return resp.data.access_token;
}

export async function verifyLineWebhook(channelSecret: string, webhookUrl?: string): Promise<boolean> {
  try {
    const token = await getChannelToken(channelSecret);
    // Lightweight channel info check to validate secret and channel
    const resp = await axios.get('https://api.line.me/v2/bot/info', {
      headers: { Authorization: `Bearer ${token}` },
      timeout: 
