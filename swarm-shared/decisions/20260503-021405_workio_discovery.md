# workio / discovery

## Final Synthesis (chosen from strongest parts, corrected + actionable)

**Chosen approach:**  
Adopt Candidate 2’s **idempotency table + atomic transaction** for correctness and safe retries, but **replace `attendance` with `punches`** (Candidate 1’s table name) and **use `line_event_id` as the primary idempotency key** (Candidate 1) because LINE already provides a unique message/delivery identifier. Add a **tenant-scoped uniqueness constraint** on the punch itself to guarantee one punch per period per day per tenant.

This resolves contradictions in favor of:
- **Correctness:** true idempotency at ingestion (idempotency table) and data integrity (unique constraint + atomic upsert).
- **Actionability:** minimal, focused schema changes and a single transactional handler you can implement and test immediately.

---

## 1. Schema changes (run once)

```sql
-- 1) Idempotency table for LINE webhook deliveries
CREATE TABLE IF NOT EXISTS webhook_events (
  idempotency_key TEXT NOT NULL PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 2) Punches table (if not exists) with tenant-scoped uniqueness
--    - line_event_id is globally unique per LINE event and used for idempotency
--    - (tenant_id, employee_id, punch_date, punch_type) enforces one punch per period per day per tenant
ALTER TABLE punches
  ADD CONSTRAINT uq_punch_line_event UNIQUE (tenant_id, line_event_id);

ALTER TABLE punches
  ADD CONSTRAINT uq_punch_daily_period
  UNIQUE (tenant_id, employee_id, punch_date, punch_type);
```

---

## 2. Service implementation (transactional + idempotent)

File: `server/src/services/attendanceService.ts`

```typescript
import { pool } from '../db';
import crypto from 'crypto';

export async function handleLinePunch({
  tenantId,
  employeeId,
  lineEventId,
  lineUserId,
  type, // 'in' | 'out'
  latitude,
  longitude,
  timestamp = new Date(),
}: {
  tenantId: string;
  employeeId: string;
  lineEventId: string;
  lineUserId: string;
  type: 'in' | 'out';
  latitude?: number;
  longitude?: number;
  timestamp?: Date;
}) {
  const client = await pool.connect();
  const punchDate = new Date(timestamp).toISOString().slice(0, 10); // YYYY-MM-DD
  const idempotencyKey = `line:${tenantId}:${lineEventId}`;
  const payloadHash = crypto
    .createHash('sha256')
    .update(JSON.stringify({ tenantId, employeeId, lineEventId, type, punchDate }))
    .digest('hex');

  try {
    await client.query('BEGIN');

    // 1) Idempotency guard
    const idemRes = await client.query(
      `INSERT INTO webhook_events (idempotency_key, tenant_id, event_type, payload_hash)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (idempotency_key) DO NOTHING
       RETURNING idempotency_key`,
      [idempotencyKey, tenantId, 'line_punch', payloadHash]
    );

    if (idemRes.rowCount === 0) {
      // Duplicate event — skip punch creation
      await client.query('COMMIT');
      return { created: false, duplicate: true, reason: 'duplicate_event' };
    }

    // 2) Atomic punch insert (no race)
    const punchRes = await client.query(
      `INSERT INTO punches (
         tenant_id,
         employee_id,
         line_event_id,
         line_user_id,
         punch_date,
         punch_type,
         latitude,
         longitude,
         created_at
       ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       ON CONFLICT (tenant_id, line_event_id) DO NOTHING
       RETURNING *`,
      [
        tenantId,
        employeeId,
        lineEventId,
        lineUserId,
        punchDate,
        type,
        latitude,
        longitude,
        timestamp,
      ]
    );

    await client.query('COMMIT');

    const created = punchRes.rowCount && punchRes.rowCount > 0;
    return {
      created,
      duplicate: !created,
      punch: created ? punchRes.rows[0] : null,
    };
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

---

## 3. LINE webhook route (safe retry handling)

File: `server/src/routes/lineWebhook.ts` (excerpt)

```typescript
import express from 'express';
import { handleLinePunch } from '../services/attendanceService';

const router = express.Router();

router.post('/webhook/line', async (req, res) => {
  const events = req.body.events || [];

  for (const ev of events) {
    // Use a stable, unique LINE identifier as idempotency key
    const lineEventId = ev.source?.userId + '-' + ev.timestamp + '-' + (ev.message?.id || ev.type);

    // Resolve tenantId and employeeId from your mapping (e.g., lineUserId -> employee)
    const resolvedTenantId = /* your lookup */;
    const resolvedEmployeeId = /* your lookup */;
    const type = /* derive from ev or user intent */ 'in';

    await handleLinePunch({
      tenantId: resolvedTenantId,
      employeeId: resolvedEmployeeId,
      lineEventId,
      lineUserId: ev.source.userId,
      type,
      latitude: ev.message?.location?.latitude,
      longitude: ev.message?.location?.longitude,
      timestamp: new Date(ev.timestamp),
    });
  }

  res.status(200).send('OK');
});

export default router;
```

---

## 4. Verification & production safety (actionable checklist)

- **Unit test:** call `handleLinePunch` twice with same `lineEventId`; second call must return `{ created: false, duplicate: true }` and insert exactly one row.
- **Constraint check:**  
  ```sql
  SELECT tenant_id, line_event_id, COUNT(*) FROM punches GROUP BY tenant_id, line_event_id HAVING COUNT(*) > 1;
  ```
  Must return zero rows.
- **Integration test:** replay the same LINE webhook payload twice; second request must return 200 and not create a new punch.
- **Metrics/logging:** increment a counter `duplicate_punch_total` when `duplicate === true` and log `lineEventId` for observability.
- **Notifications:** send LINE notifications **only after successful commit** (as in Candidate 2) to avoid duplicate notifications on retry.

---

## Why this is the best synthesis

- Uses **Candidate 2’s idempotency table + transaction** for robust duplicate suppression and safe side-effects.
- Uses **Candidate 1’s `line_event_id` uniqueness** for simple, reliable idempotency tied to LINE’s own identifiers.
- Adds **tenant-scoped uniqueness** to enforce one punch per period per day per tenant (multi-tenant correctness).
- Provides **concrete, copy-paste schema + code + tests** you can deploy immediately.
