# workio / discovery

## Final synthesized implementation (best of both candidates)

**Core principle**: enforce idempotency at the database level with a tenant-scoped uniqueness key and atomic upsert, while keeping the webhook handler simple, safe, and observable.

---

### 1. Schema (run once)

Add a nullable `line_event_id` to the attendance/punches table and enforce tenant-scoped uniqueness only when the key is present. This allows gradual rollout and avoids blocking existing rows.

```sql
-- Add column if not present
ALTER TABLE attendance_punches
  ADD COLUMN IF NOT EXISTS line_event_id TEXT NULL;

-- Tenant-scoped uniqueness for idempotency (only when key is provided)
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_punches_tenant_user_line_event
  ON attendance_punches (tenant_id, user_id, line_event_id)
  WHERE line_event_id IS NOT NULL;

-- Optional: fast lookup for dedupe checks and audits
CREATE INDEX IF NOT EXISTS idx_attendance_punches_tenant_user_time
  ON attendance_punches (tenant_id, user_id, timestamp DESC);
```

---

### 2. Idempotent service function

Use a single atomic `INSERT ... ON CONFLICT DO NOTHING` and return the existing row when a duplicate is detected. Keep the transaction short and rollback on any error.

File: `server/src/services/attendanceService.ts`

```ts
import { pool } from '../db/index.js';

export async function recordPunch({
  tenantId,
  userId,
  lineEventId,
  type,
  timestamp = new Date(),
  location = null,
}: {
  tenantId: string;
  userId: string;
  lineEventId: string | null;
  type: 'in' | 'out';
  timestamp?: Date;
  location?: { lat: number; lng: number } | null;
}) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Atomic upsert: if line_event_id is provided and conflicts, do nothing
    const upsert = await client.query(
      `INSERT INTO attendance_punches (tenant_id, user_id, type, timestamp, location, line_event_id)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (tenant_id, user_id, line_event_id) DO NOTHING
       RETURNING id, type, timestamp, location, line_event_id`,
      [tenantId, userId, type, timestamp, location, lineEventId]
    );

    // If insert happened, commit and return new row
    if (upsert.rowCount && upsert.rowCount > 0) {
      await client.query('COMMIT');
      return upsert.rows[0];
    }

    // Conflict or no line_event_id: fetch existing row (most recent for this tenant+user+line_event_id)
    const existing = await client.query(
      `SELECT id, type, timestamp, location, line_event_id
       FROM attendance_punches
       WHERE tenant_id = $1 AND user_id = $2 AND line_event_id = $3
       ORDER BY timestamp DESC
       LIMIT 1`,
      [tenantId, userId, lineEventId]
    );

    await client.query('COMMIT');
    return existing.rows[0] || null;
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

---

### 3. Webhook handler

Extract a deterministic `lineEventId` from the LINE event (or generate one if missing), and call `recordPunch`. Keep handler logic minimal; do not swallow errors silently.

File: `server/src/controllers/lineWebhook.ts`

```ts
import { Request, Response } from 'express';
import { recordPunch } from '../services/attendanceService.js';

export async function handleLineWebhook(req: Request, res: Response) {
  const events = req.body?.events;
  const tenantId = req.headers['x-tenant-id'] || process.env.DEFAULT_TENANT_ID;

  if (!events?.length) return res.sendStatus(200);

  const results = [];

  for (const ev of events) {
    try {
      // Deterministic idempotency key from LINE event (or fallback)
      const lineEventId =
        ev.source?.userId && ev.timestamp
          ? `${ev.source.userId}-${ev.timestamp}-${ev.type}-${ev.message?.id || ''}`
          : null;

      const type = ev.message?.text?.toLowerCase().includes('out') ? 'out' : 'in';

      const punch = await recordPunch({
        tenantId,
        userId: ev.source.userId,
        lineEventId,
        type,
        timestamp: ev.timestamp ? new Date(ev.timestamp) : new Date(),
        location: ev.location || null,
      });

      results.push({ event: ev.type, punchId: punch?.id || null, duplicate: !punch?.id });
    } catch (err) {
      // Log but continue processing other events
      console.error('Failed to process LINE event', { err, event: ev });
    }
  }

  return res.status(200).json({ ok: true, results });
}
```

---

### 4. Verification checklist

- **Duplicate payload test**: POST identical LINE payload twice; second request must return the existing punch row and not create a new one.
- **Concurrency test**: fire 10 parallel requests with the same `lineEventId`; confirm exactly one punch row exists afterward.
- **Constraint test**: attempt direct DB insert with duplicate `(tenant_id, user_id, line_event_id)` where `line_event_id` is not null; must be rejected.
- **Rollback test**: force an error after the upsert (e.g., in a follow-up step) and confirm no partial rows remain.
- **Null key behavior**: ensure events without `lineEventId` still create punches (no constraint violation) but are not idempotent; document this trade-off.

---

### 5. Concrete next steps

1. Apply the schema migration in staging and verify index/constraint behavior.
2. Deploy `recordPunch` and `handleLineWebhook` behind a feature flag or in a canary release.
3. Add unit tests for duplicate detection and concurrency scenarios.
4. Monitor logs and metrics for duplicate event rates and DB constraint violations.
5. Once stable, remove any legacy non-atomic upsert paths and enforce `line_event_id` population at the webhook boundary.
